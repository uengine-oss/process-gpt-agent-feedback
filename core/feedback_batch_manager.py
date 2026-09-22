"""
피드백 배치 수집/트리거/제안 승인 실행 매니저.

두 개의 독립된 루프로 구성된다:
  - start_feedback_batch_collection: agent_feedback_task RPC로 피드백이 있는 완료된
    워크아이템을 가져와, 그 워크아이템에서 새로 생긴 피드백 전부를 한 단위로
    (tenant_id, proc_def_id, activity_id) 배치에 적재만 한다. 스킬 조회나 스킬 개선은
    하지 않는다.
  - start_feedback_batch_trigger: COLLECTING 배치들을 주기적으로 확인해 트리거
    조건(워크아이템 수 또는 경과 시간)을 충족하면 배치를 CLASSIFYING으로 닫고
    무엇을 개선할 수 있는지 분류하고
    (SKILL/DMN_RULE/PROCESS_DEFINITION, 여러 개 동시 가능) target별 제안을
    만들어 제안(PROPOSED)으로 전환하거나, 공통 관심사가 없으면 배치를
    폐기(DISCARDED)한다.

제안의 각 target은 독립적으로 승인/거절된다. 승인된 SKILL target을 실제 스킬 개선
파이프라인(process_feedback_with_deep_agent, 기존 HTTP 기반 skill_api_client
경로)에 태우는 apply_approved_proposal은 API 호출로 발생하는 이벤트이므로 폴링
루프가 아니라 여기서 직접 처리한다. 승인된 DMN_RULE target은 apply_approved_dmn_target이,
승인된 PROCESS_DEFINITION target은 apply_approved_process_definition_target이
각각 draft proc_def_version + resource_pull_requests 병합 요청을 만든다 — 둘 다
라이브 proc_def.definition은 건드리지 않는다(add-feedback-proposal-apply /
add-process-definition-apply design.md 참고).
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from utils.logger import log, handle_error
from core.database import (
    append_workitem_feedback_to_batch,
    claim_batch_for_classification,
    fetch_stale_classifying_batches,
    touch_classifying_batch,
    extract_new_feedback_items,
    fetch_collecting_batches,
    fetch_feedback_task,
    fetch_proc_def_name,
    fetch_todolist_rows_by_ids,
    list_agent_dmn_rules,
    load_activity_skills,
    mark_batch_discarded,
    mark_batch_proposed,
    mark_feedback_collected_count,
    patch_target,
    update_feedback_status,
    _get_agent_by_id,
    _get_dmn_definition_from_xml,
    _get_proc_def_definition,
    _get_proc_def_bpmn_xml,
    merge_dmn_artifact_into_definition,
    merge_process_definition_artifact_into_definition,
    compute_next_draft_version,
    insert_draft_proc_def_version,
    insert_dmn_merge_request,
    insert_bpmn_merge_request,
)
from core.feedback_processor import (
    classify_and_extract_proposal,
    is_noise_feedback,
    match_feedback_to_agents,
    resolve_dmn_identity,
    resolve_skill_identity,
)
from core.polling_manager import get_agents_info
from core.deep_agent import process_feedback_with_deep_agent
from core.dmn_xml import dmn_decisions_rules_to_xml
from core.bpmn_xml import merge_process_definition_artifact_into_xml
from core.apply_tracking import start_skill_commit_log
from core.skill_api_client import use_auth_token

# 배치 트리거 임계값 — 워크아이템 5개 또는 3일 중 먼저 오는 조건 (튜닝 가능한 전역 상수로 시작)
BATCH_TRIGGER_COUNT = 5
BATCH_TRIGGER_MAX_AGE = timedelta(days=3)
# CLASSIFYING에 이만큼 머문 배치는 분류가 끊긴 것으로 보고 다시 분류한다.
CLASSIFYING_RETRY_AFTER_MINUTES = 15

# 배치 전체 이벤트 로그 상한 (여러 워크아이템의 이벤트를 합치므로 최신순으로 상한을 둔다)
_MAX_EVENTS_PER_BATCH = 100
_STRATEGY_SERVICE_URL = os.getenv("STRATEGY_SERVICE_URL", "http://localhost:8014")


def _target_alignment_description(target: Dict[str, Any]) -> str:
    artifact = target.get("artifact")
    if isinstance(artifact, str):
        return artifact
    return json.dumps(artifact or {}, ensure_ascii=False)


async def _attach_alignment_evidence(batch: Dict[str, Any], target: Dict[str, Any]) -> None:
    """승인 가능한 SKILL/PROCESS_DEFINITION target에 정합성 근거를 붙인다."""
    if target.get("type") == "DMN_RULE":
        return
    body = {"description": _target_alignment_description(target)}
    if target.get("type") == "PROCESS_DEFINITION":
        body["proc_def_id"] = batch.get("proc_def_id")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            response = await client.post(
                f"{_STRATEGY_SERVICE_URL.rstrip('/')}/api/ai/alignment",
                params={"tenant_id": batch.get("tenant_id", "")}, json=body,
            )
            response.raise_for_status()
            target["alignment_evidence"] = response.json()
    except Exception as exc:  # 조회 실패가 제안 생성을 막아서는 안 된다.
        target["alignment_evidence"] = {
            "status": "unavailable",
            "candidates": [],
            "existing_connections": [],
            "reason": f"전략 정합성 확인 불가: {type(exc).__name__}",
        }


# ---------------------------------------------------------------------------
# 1. 수집 루프 — 피드백을 즉시 처리하지 않고 배치에 적재만 한다
# ---------------------------------------------------------------------------

async def process_feedback_collection_task(row: Dict[str, Any]) -> None:
    """완료된 워크아이템 1건에서 아직 수집하지 않은 피드백 전부를 한 단위로 해당
    (tenant_id, proc_def_id, activity_id) 배치에 적재한다.

    처리 단위는 피드백 한 건이 아니라 워크아이템이다. 한 워크아이템에 피드백이 몇 건 쌓였든
    새로 생긴 것들은 한 번의 적재(append_workitem_feedback_to_batch)로 같은 배치에 들어간다.
    그 배치가 이미 분류로 넘어간 뒤에 같은 워크아이템에 남긴 피드백은
    feedback_collected_count 이후 항목(extract_new_feedback_items)으로 다시 집혀 다음
    COLLECTING 배치, 즉 다음 처리 회차로 간다.

    스킬 탐색·스킬 개선은 하지 않는다 — 그건 배치가 트리거되어 사용자가 제안을
    승인한 뒤 apply_approved_proposal()이 담당한다.
    """
    todo_id = row["id"]
    collected_count = row.get("feedback_collected_count") or 0
    new_items = extract_new_feedback_items(row.get("feedback", ""), collected_count)
    tenant_id = str(row.get("tenant_id") or "").strip()
    proc_def_id = str(row.get("proc_def_id") or "").strip()
    activity_id = str(row.get("activity_id") or "").strip()

    if not new_items:
        log(f"⚠️ 새로 수집할 피드백 없음, 건너뜀: todo_id={todo_id}")
        await update_feedback_status(todo_id, "FAILED")
        return

    # 처리 시작 전에 먼저 개수를 올려둔다 — RPC의 "배열 길이 > feedback_collected_count"
    # 조건이 feedback_status와 무관하게 재조회를 허용하므로, 처리 도중 다음 폴링 틱이
    # 같은 항목을 다시 집어가는 것을 막기 위함이다.
    await mark_feedback_collected_count(todo_id, collected_count + len(new_items))

    units = [
        {"todo_id": todo_id, "content": content, "time": time, "user_id": user_id}
        for content, user_id, time in new_items
        if content
    ]
    try:
        batch = await append_workitem_feedback_to_batch(tenant_id, proc_def_id, activity_id, units)
        if not batch:
            log(f"⚠️ 배치 적재 실패: todo_id={todo_id}")
            await update_feedback_status(todo_id, "FAILED")
            return

        await update_feedback_status(todo_id, "COLLECTED")
        log(f"✅ 피드백 수집 완료: todo_id={todo_id}, 피드백 {len(units)}건, batch_id={batch.get('id')}")
    except Exception as e:
        log(f"⚠️ 피드백 수집 에러: todo_id={todo_id}")
        handle_error("피드백수집", e)
        await update_feedback_status(todo_id, "FAILED")


async def start_feedback_batch_collection(interval: int = 7) -> None:
    """피드백 수집 폴링을 시작한다. 서버 수명 동안 무한 루프로 실행된다."""
    log(f"피드백 수집 폴링 시작 (interval={interval}s)")

    while True:
        try:
            row = await fetch_feedback_task()
            if row:
                await process_feedback_collection_task(row)
        except asyncio.CancelledError:
            log("피드백 수집 폴링 종료")
            break
        except Exception as e:
            log("⚠️ 폴링 중 에러 (계속 진행)")
            handle_error("피드백수집폴링", e)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# 2. 배치 트리거 루프 — 건수/경과 시간 조건 확인 후 규칙 추출
# ---------------------------------------------------------------------------

def batch_workitem_count(collected_items: List[Dict[str, Any]]) -> int:
    """배치에 담긴 워크아이템 수. 한 워크아이템의 피드백이 여러 건이어도 하나로 센다."""
    return len({str(item.get("todo_id")) for item in collected_items if item.get("todo_id")})


def is_batch_triggered(collected_items: List[Dict[str, Any]], first_collected_at: str) -> bool:
    """워크아이템 5개 또는 경과 시간 3일 중 먼저 오는 조건을 충족했는지 확인한다.

    피드백 건수가 아니라 워크아이템 수를 센다 — 한 워크아이템에 피드백을 다섯 번 남긴 것은
    서로 다른 업무 다섯 건에서 같은 문제가 드러난 것과 다르다.

    DB/LLM 호출 없이 순수하게 판단하므로 단위 테스트가 쉽다.
    """
    if batch_workitem_count(collected_items) >= BATCH_TRIGGER_COUNT:
        return True

    if not first_collected_at:
        return False
    try:
        first_dt = datetime.fromisoformat(str(first_collected_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if first_dt.tzinfo is None:
        first_dt = first_dt.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc) - first_dt >= BATCH_TRIGGER_MAX_AGE


def _union_user_ids(rows: List[Dict[str, Any]]) -> str:
    ids = set()
    for row in rows:
        for uid in str(row.get("user_id") or "").split(","):
            uid = uid.strip()
            if uid:
                ids.add(uid)
    return ",".join(sorted(ids))


def _union_assignees(rows: List[Dict[str, Any]]) -> List[Any]:
    import json as _json
    merged: List[Any] = []
    for row in rows:
        raw = row.get("assignees")
        try:
            data = _json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            merged.extend(data)
    return merged


def _parse_comma_skills(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return [s.strip() for s in text.split(",") if s.strip()]


async def _representative_agent(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """배치 워크아이템 중 가장 최근(end_date/updated_at) 것을 담당한 에이전트 1명을
    대표로 고른다 — target.id/name 미리보기 계산에만 쓴다. 실제 승인 처리 시점에는
    match_feedback_to_agents로 배치 전체 워크아이템의 담당 에이전트를 다시 판단한다.
    """
    sorted_rows = sorted(rows, key=lambda r: r.get("end_date") or r.get("updated_at") or "", reverse=True)
    for row in sorted_rows:
        agents = await get_agents_info(_union_user_ids([row]), _union_assignees([row]))
        if agents:
            return agents[0]
    return None


def _feedback_author_ids(collected_items: List[Dict[str, Any]]) -> List[str]:
    """피드백을 남긴 모든 user_id를 최초 기여 시각 순으로, 중복 없이 반환한다.
    resource_pull_requests.requester_id(uuid[])에 그대로 들어간다 — 이 배치의
    개선을 촉발한 사람들이 requester다(승인자는 reviewer_id로 별도 기록)."""
    items_sorted = sorted(collected_items, key=lambda x: x.get("time", ""))
    seen: Dict[str, None] = {}
    for item in items_sorted:
        uid = str(item.get("user_id") or "").strip()
        if uid and uid not in seen:
            seen[uid] = None
    return list(seen.keys())


_MAX_DMN_RULE_EXAMPLES = 6


def _describe_dmn(definition: Optional[Dict[str, Any]]) -> str:
    """DMN 한 개의 판단 대상을 사람이 읽는 한 단락으로 요약한다(의사결정·입력·출력·규칙 예)."""
    if not definition:
        return ""
    parts = []
    for d in definition.get("dmn_decisions") or []:
        inputs = ", ".join(i.get("label") or i.get("expression") or "" for i in d.get("inputs") or [])
        output = (d.get("output") or {})
        out_name = output.get("name") or output.get("label") or ""
        parts.append(f"{d.get('name', '')}(입력: {inputs} → 출력: {out_name})")
    rules = [
        f"{r.get('decision_name', '')}: {r.get('when', '')} → {r.get('then', '')}"
        for r in (definition.get("dmn_rules") or [])[:_MAX_DMN_RULE_EXAMPLES]
    ]
    text = "의사결정: " + "; ".join(parts) if parts else ""
    if rules:
        text += " / 규칙 예: " + "; ".join(rules)
    return text


def _agent_dmn_candidates(tenant_id: str, agent_id: str) -> List[Dict[str, Any]]:
    """에이전트 소유 DMN 후보에 실제 규칙 내용을 설명으로 붙인다.

    proc_def에는 description 컬럼이 없어 후보가 이름만 가졌고, 식별 LLM이 이름만 보고
    판단하다 보니 "할인율 결정" 제안이 할인율을 정하는 "고객 혜택 결정"을 못 알아보고
    PASS로 빠지는 일이 있었다(실제 발생: batch_id=a783b8c1-5d5b-4dfd-8de9-470b0ce59c47).
    """
    candidates = list_agent_dmn_rules(tenant_id, agent_id)
    for c in candidates:
        c["description"] = _describe_dmn(_get_dmn_definition_from_xml(tenant_id, c.get("id", "")))
    return candidates


async def _fill_target_identity(batch: Dict[str, Any], target: Dict[str, Any]) -> bool:
    """target(SKILL/DMN_RULE/PROCESS_DEFINITION)이 가리킬 수 있는 기존 리소스가 실제로
    있는지 확인하고, 있으면 target["id"]/["name"]을 채운다.

    이 시스템은 피드백 기반 "개선"만 다룬다 — 신규 생성 경로는 없다. 매칭되는 기존
    리소스가 없거나(PASS 판정) 이미 삭제된 경우 False를 반환해 호출부가 이 target을
    제안에서 아예 제외하게 한다.

    PROCESS_DEFINITION은 배치의 proc_def_id 자체이므로 바로 확정된다. SKILL/DMN_RULE은
    배치의 대표 에이전트(없으면 활동 귀속) 기준으로 한 번만 계산하는 미리보기 값이다 —
    실제 승인 처리는 에이전트별로 다시 판단한다(apply_approved_proposal/apply_approved_dmn_target).
    """
    ttype = target.get("type")
    tenant_id = batch.get("tenant_id", "")

    if ttype == "PROCESS_DEFINITION":
        proc_def_id = batch.get("proc_def_id", "")
        name = fetch_proc_def_name(tenant_id, proc_def_id)
        if not name:
            target["drop_reason"] = "개선할 프로세스 정의를 찾지 못했습니다"
            return False
        target["id"] = proc_def_id
        target["name"] = name
        return True

    items = batch.get("collected_items") or []
    todo_ids = [item.get("todo_id") for item in items if item.get("todo_id")]
    rows = await fetch_todolist_rows_by_ids(todo_ids)
    agent = await _representative_agent(rows)

    if ttype == "SKILL":
        if agent:
            candidate_names = _parse_comma_skills(agent.get("skills"))
        else:
            candidate_names = load_activity_skills(
                tenant_id=tenant_id,
                proc_def_id=batch.get("proc_def_id", ""),
                activity_id=batch.get("activity_id", ""),
            )
        candidates = [{"name": n, "description": ""} for n in candidate_names]
        if not candidates:
            target["drop_reason"] = "이 활동의 담당 에이전트·활동에 연결된 스킬이 없습니다"
            return False
        resolved = await resolve_skill_identity(str(target.get("artifact") or ""), candidates)
        if resolved["decision"] != "UPDATE":
            target["drop_reason"] = "연결된 스킬 중 이 내용을 개선할 스킬을 찾지 못했습니다"
            return False
        target["id"] = resolved["name"]
        target["name"] = resolved["name"]
        # skill_name: process-gpt-vue3의 이미 배포된 skill-proposal-indicator 기능이
        # 사이드바/스킬 관리 페이지의 "!" 배지를 이 필드(tenant_skills.skill_name과 동일
        # 문자열)로 매칭한다(useSkillProposals.js buildSkillProposalMap). id/name과 항상
        # 같은 값이지만, 그 기존 계약을 깨지 않기 위해 별도로 채운다.
        target["skill_name"] = resolved["name"]
        return True

    if ttype == "DMN_RULE":
        # DMN은 agent_id로만 후보를 조회한다(list_agent_dmn_rules) — 담당 에이전트가
        # 없으면 비교할 기존 리소스가 애초에 없으므로 PASS 외의 결과가 나올 수 없다.
        if not agent:
            target["drop_reason"] = "담당 에이전트가 없어 개선할 DMN 규칙을 특정할 수 없습니다"
            return False
        artifact = target.get("artifact") or {}
        candidates = _agent_dmn_candidates(tenant_id, agent.get("id", ""))
        if not candidates:
            target["drop_reason"] = "담당 에이전트에 연결된 DMN 규칙이 없습니다"
            return False
        resolved = await resolve_dmn_identity(artifact, candidates)
        if resolved["decision"] != "UPDATE" or not resolved.get("id"):
            target["drop_reason"] = "연결된 DMN 규칙 중 이 내용을 개선할 규칙을 찾지 못했습니다"
            return False
        target["id"] = resolved["id"]
        target["name"] = resolved["name"]
        return True

    return False


DISCARD_REASON_NO_CONTENT = "칭찬·감사·다시 해달라는 요청처럼 개선할 내용이 담기지 않은 피드백만 모였습니다"
DISCARD_REASON_NO_COMMON_CONCERN = "함께 모인 피드백에서 스킬·DMN 규칙·프로세스 정의로 개선할 공통점을 찾지 못했습니다"
DISCARD_REASON_NO_EXISTING_RESOURCE = "개선할 점은 찾았지만 반영할 기존 스킬·DMN 규칙·프로세스 정의가 없습니다"


async def _process_triggered_batch(batch: Dict[str, Any]) -> None:
    batch_id = batch["id"]
    items = batch.get("collected_items") or []

    meaningful = [item for item in items if not is_noise_feedback(item.get("content", ""))]
    targets = await classify_and_extract_proposal(meaningful, task_description="") if meaningful else []
    if targets is None:
        # 분류 호출 자체가 실패했다 — "공통점 없음"으로 폐기하면 피드백을 잃는다.
        # CLASSIFYING에 남겨 두고 일정 시간 뒤 다시 분류한다(start_feedback_batch_trigger).
        touch_classifying_batch(batch_id)
        log(f"⚠️ 배치 분류 실패, 나중에 다시 분류: batch_id={batch_id}")
        return

    async def _discard(reason: str, dropped: List[Dict[str, Any]]) -> None:
        if await mark_batch_discarded(batch_id, reason=reason, dropped_targets=dropped):
            for item in items:
                todo_id = item.get("todo_id")
                if todo_id:
                    await update_feedback_status(todo_id, "REJECTED")
            log(f"배치 폐기({reason}): batch_id={batch_id}")

    if not meaningful:
        await _discard(DISCARD_REASON_NO_CONTENT, [])
        return
    if not targets:
        await _discard(DISCARD_REASON_NO_COMMON_CONCERN, [])
        return

    kept_targets = []
    dropped_targets = []
    for target in targets:
        if await _fill_target_identity(batch, target):
            await _attach_alignment_evidence(batch, target)
            kept_targets.append(target)
        else:
            target.setdefault("drop_reason", "개선할 기존 리소스를 찾지 못했습니다")
            dropped_targets.append(target)

    if not kept_targets:
        await _discard(DISCARD_REASON_NO_EXISTING_RESOURCE, dropped_targets)
        return

    # candidate_skill_names: SKILL target.id/name이 대표값을 이미 담으므로 더는 계산하지
    # 않는다. 컬럼/응답 필드 자체는 다른 소비자를 위해 남겨두고 항상 빈 배열만 전달한다.
    if await mark_batch_proposed(batch_id, kept_targets, [], dropped_targets=dropped_targets):
        target_types = [t.get("type") for t in kept_targets]
        log(f"배치 제안 생성: batch_id={batch_id}, targets={target_types}, dropped={len(dropped_targets)}")


async def start_feedback_batch_trigger(interval: int = 900) -> None:
    """COLLECTING 배치의 트리거 조건을 주기적으로 확인한다. 서버 수명 동안 무한 루프로 실행된다."""
    log(f"피드백 배치 트리거 확인 시작 (interval={interval}s)")

    while True:
        try:
            batches = await fetch_collecting_batches()
            for batch in batches:
                if not is_batch_triggered(batch.get("collected_items") or [], batch.get("first_collected_at", "")):
                    continue
                # 분류 전에 배치를 닫는다. 닫힌 시점의 항목만 이번 회차에 분류되고,
                # 그 뒤 들어온 피드백은 새 COLLECTING 배치(다음 회차)로 간다.
                claimed = await claim_batch_for_classification(batch["id"])
                if claimed:
                    await _process_triggered_batch(claimed)

            # 분류 도중 서버가 내려갔거나 분류 호출이 실패해 CLASSIFYING에 멈춘 배치를 다시 분류한다.
            for batch in await fetch_stale_classifying_batches(CLASSIFYING_RETRY_AFTER_MINUTES):
                await _process_triggered_batch(batch)
        except asyncio.CancelledError:
            log("피드백 배치 트리거 확인 종료")
            break
        except Exception as e:
            log("⚠️ 배치 트리거 확인 중 에러 (계속 진행)")
            handle_error("배치트리거확인", e)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# 3. 승인된 제안 실행 — 기존 피드백→스킬 개선 파이프라인(HTTP 기반)에 그대로 태운다
# ---------------------------------------------------------------------------

def _representative_description(rows: List[Dict[str, Any]]) -> str:
    """가장 최근에 종료/갱신된 워크아이템의 description을 대표값으로 쓴다."""
    if not rows:
        return ""
    latest = max(rows, key=lambda r: r.get("end_date") or r.get("updated_at") or "")
    return latest.get("description", "") or ""


async def apply_approved_proposal(
    batch: Dict[str, Any],
    extracted_rule: str,
    bound_skill_name: Optional[str] = None,
    approver_id: Optional[str] = None,
    approver_name: Optional[str] = None,
) -> Dict[str, Any]:
    """승인된 SKILL target을 기존 피드백→스킬 개선 파이프라인(process_feedback_with_deep_agent)에 태운다.

    이 함수는 제안의 SKILL target이 승인됐을 때만 호출된다 — DMN_RULE/PROCESS_DEFINITION
    target 승인은 결정 기록만 하고 여기로 오지 않는다(core.feedback_proposal_routes 참고).
    extracted_rule은 그 SKILL target의 artifact(자연어 일반 규칙 텍스트)다. bound_skill_name은
    제안 생성 시점에 이미 확정된 스킬 이름(target.name)으로, 매칭된 모든 에이전트가 새 이름을
    짓지 않고 이 이름을 그대로 쓰도록 강제한다. approver_id는 이 target을 승인한 사람으로,
    스킬 병합 요청의 reviewer가 되며(requester는 batch의 피드백 작성자들이다,
    fix-merge-request-requester), 배치의 원 피드백 작성자들과 함께 스킬 기여 이력
    (skill_contributions)에도 기록된다(agent-feedback_skill-contribution-tracking).

    반환값은 적용 결과 {"apply_status", "apply_results", "apply_error"}다 — 실제로 커밋된
    스킬과 그 병합 요청은 Deep Agent 도구 호출이 남긴 커밋 기록(core.apply_tracking)에서
    읽는다. 워크아이템 feedback_status와 제안 target 기록은 호출부(run_target_apply)가 한다.

    스킬 커밋은 process-gpt-agent-feedback이 원래 갖고 있던 HTTP API 경로(core/deep_agent.py
    + core/skill_tools.py)를 그대로 사용한다 — git PR 방식으로 새로 만들지 않는다.
    """
    batch_id = batch["id"]
    items = batch.get("collected_items") or []
    todo_ids = [item.get("todo_id") for item in items if item.get("todo_id")]
    requester_ids = _feedback_author_ids(items)

    if not extracted_rule:
        log(f"⚠️ 승인된 SKILL target에 artifact가 없음: batch_id={batch_id}")
        return {"apply_status": "FAILED", "apply_results": [], "apply_error": "개선 내용이 비어 있습니다"}

    commit_log = start_skill_commit_log()
    errors: List[str] = []

    rows = await fetch_todolist_rows_by_ids(todo_ids)
    user_ids = _union_user_ids(rows)
    assignees = _union_assignees(rows)
    description = _representative_description(rows)

    # 스킬 기여자 = 원 피드백 작성자들 + 이 target을 승인한 사람(중복 제거, 순서 유지).
    contributor_user_ids = list(
        dict.fromkeys([u for u in user_ids.split(",") if u] + ([approver_id] if approver_id else []))
    )

    from core.database import fetch_events_by_todo_id
    events: List[Dict[str, Any]] = []
    for todo_id in todo_ids:
        events.extend(await fetch_events_by_todo_id(todo_id))
    events.sort(key=lambda e: e.get("timestamp", ""))
    if len(events) > _MAX_EVENTS_PER_BATCH:
        events = events[-_MAX_EVENTS_PER_BATCH:]

    agents = await get_agents_info(user_ids, assignees)

    if agents:
        from core.feedback_processor import match_feedback_to_agents
        matching = await match_feedback_to_agents(extracted_rule, agents, description, events)
        agent_feedbacks = matching.get("agent_feedbacks", [])

        if not agent_feedbacks:
            log(f"매칭된 피드백 없음: batch_id={batch_id}")
        for fb_item in agent_feedbacks:
            aid = fb_item.get("agent_id")
            aname = fb_item.get("agent_name", "Unknown")
            lc = fb_item.get("learning_candidate", {})
            if not lc:
                continue

            agent_info = _get_agent_by_id(aid)
            if not agent_info:
                continue

            try:
                result = await process_feedback_with_deep_agent(
                    agent_id=aid,
                    agent_info=agent_info,
                    feedback_content=lc.get("content", ""),
                    task_description=description,
                    events=events,
                    bound_skill_name=bound_skill_name,
                    requester_ids=requester_ids,
                    reviewer_id=approver_id,
                    contributor_user_ids=contributor_user_ids,
                    contribution_source="proposal_approval",
                )
                if result.get("error"):
                    errors.append(str(result["error"])[:300])
                    log(f"⚠️ 에이전트 {aname} 처리 에러: {str(result['error'])[:200]}")
            except Exception as e:
                errors.append(str(e)[:300])
                log(f"⚠️ 에이전트 {aname} 피드백 처리 실패")
                handle_error(f"피드백처리({aname})", e)
    else:
        # 담당 에이전트가 없는 경우: match_feedback_to_agents로 매칭할 대상이 없으므로
        # extracted_rule을 그대로 활동 전용 Deep Agent 경로에 넘긴다. 스킬은
        # proc_def.definition.activities[].skills에 귀속된다 (add-feedback-proposal-apply).
        activity_ref = {
            "tenant_id": batch.get("tenant_id", ""),
            "proc_def_id": batch.get("proc_def_id", ""),
            "activity_id": batch.get("activity_id", ""),
        }
        try:
            result = await process_feedback_with_deep_agent(
                feedback_content=extracted_rule,
                task_description=description,
                events=events,
                activity_ref=activity_ref,
                bound_skill_name=bound_skill_name,
                requester_ids=requester_ids,
                reviewer_id=approver_id,
                contributor_user_ids=contributor_user_ids,
                contribution_source="proposal_approval",
            )
            if result.get("error"):
                errors.append(str(result["error"])[:300])
                log(f"⚠️ 활동 전용 경로 처리 에러: {str(result['error'])[:200]}")
        except Exception as e:
            errors.append(str(e)[:300])
            log(f"⚠️ 활동 전용 경로 피드백 처리 실패: batch_id={batch_id}")
            handle_error("피드백처리(활동전용)", e)

    applied = [c for c in commit_log if c.get("committed") and c.get("pr_created")]
    failed = [c for c in commit_log if c not in applied]
    errors.extend(c.get("error") for c in failed if c.get("error"))
    if applied:
        apply_status = "APPLIED"
    elif failed or errors:
        apply_status = "FAILED"
    else:
        apply_status = "NO_CHANGE"

    log(f"승인된 SKILL target 처리 완료: batch_id={batch_id}, status={apply_status}, commits={len(commit_log)}")
    return {
        "apply_status": apply_status,
        "apply_results": commit_log,
        "apply_error": "; ".join(dict.fromkeys(errors)) or None,
    }


# ---------------------------------------------------------------------------
# 4. 승인된 DMN_RULE target 실행 — 실제 DMN 리소스(proc_def.type='dmn')에 draft
#    proc_def_version + resource_pull_requests 병합 요청을 만든다. 라이브 proc_def는
#    건드리지 않는다. 이미 승인된 target["id"]에 먼저 적용하고, SKILL(apply_approved_
#    proposal)과 동일하게 그 외 담당 에이전트에 대해서만 추가로 팬아웃한다 — DMN은
#    agent_id로 에이전트에 귀속되기 때문이다.
# ---------------------------------------------------------------------------

def _dmn_artifact_as_text(artifact: Dict[str, Any]) -> str:
    decision = artifact.get("decision") or {}
    lines = [f"의사결정: {decision.get('name', '')} - {decision.get('description', '')}"]
    for rule in artifact.get("rules") or []:
        if isinstance(rule, dict):
            lines.append(f"- {rule.get('when', '')} → {rule.get('then', '')}")
    return "\n".join(lines)


async def apply_approved_dmn_target(
    batch: Dict[str, Any],
    target: Dict[str, Any],
    approver_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """승인된 DMN_RULE target을 기존 DMN 리소스(들)에 반영한다.

    target["id"]/["name"]은 제안 생성 시점(_fill_target_identity)에 대표 에이전트
    기준으로 이미 식별되어 사람이 승인한 기존 DMN이다 — 그 매칭을 여기서 다시 LLM으로
    재판단하지 않고 그대로 적용한다. resolve_dmn_identity는 이름만으로(proc_def에
    description 컬럼이 없음) 판단하는 약한 근거의 LLM 호출이라 같은 입력에도 매번 같은
    결론을 보장하지 않는데, 승인 시점에 다시 돌리면 이미 승인된 매칭이 조용히 PASS로
    뒤집혀 개선이 통째로 스킵될 수 있었다(실제 발생 사례: batch_id=a4a21f45-e3e1-4edd-
    b96a-6817a4a0fc8a).

    배치가 대표 에이전트 외의 다른 에이전트에도 걸쳐 있을 수 있으므로, 그 외 에이전트가
    소유한 기존 DMN(agent_id=해당 에이전트, type='dmn') 중 이번 피드백과 겹치는 것이
    있는지는 추가로 찾아 팬아웃한다(이미 적용한 dmn_id는 건너뜀) — 신규 DMN 생성 경로는
    없다: 피드백 기반 "개선"만 다루는 이 시스템에서, 비교할 기존 DMN이 없으면(PASS 판정)
    그 에이전트는 건너뛴다. 라이브 proc_def는 쓰지 않는다: draft 생성과 PR 오픈까지가 이
    함수의 책임이다(add-feedback-proposal-apply design.md Non-Goals). 여러 에이전트에
    걸치면 여러 draft/PR이 만들어질 수 있어 결과를 리스트로 반환한다.

    병합 요청의 requester는 배치의 피드백 작성자들(collected_items의 user_id,
    중복 제거), reviewer는 approver_id다 — 여러 draft/PR로 팬아웃해도 모두 동일한
    값을 쓴다(fix-merge-request-requester).
    """
    batch_id = batch.get("id")
    tenant_id = batch.get("tenant_id", "")
    artifact = target.get("artifact") or {}
    decision_name = (artifact.get("decision") or {}).get("name", "")
    collected_items = batch.get("collected_items") or []
    todo_ids = [item.get("todo_id") for item in collected_items if item.get("todo_id")]
    requester_ids = _feedback_author_ids(collected_items)

    async def _apply_for_owner(dmn_id: str, owner_label: str) -> Dict[str, Any]:
        live_definition = _get_dmn_definition_from_xml(tenant_id, dmn_id)
        if live_definition is None:
            log(f"⚠️ 개선 대상 DMN이 더는 존재하지 않음, 건너뜀: batch_id={batch_id}, dmn_id={dmn_id}")
            return {"applied": False, "error": "dmn_not_found", "owner": owner_label}

        merged_definition = merge_dmn_artifact_into_definition(live_definition, artifact)

        new_version = compute_next_draft_version(tenant_id, dmn_id)
        xml_snapshot = dmn_decisions_rules_to_xml(
            merged_definition.get("dmn_decisions", []),
            merged_definition.get("dmn_rules", []),
            proc_def_id=dmn_id,
            # 표가 읽는 변수의 출처다 — 빠뜨리면 규칙 하나 더한 draft 가 입력 데이터를
            # 통째로 지운 것으로 저장된다.
            input_data=merged_definition.get("dmn_input_data") or [],
        )

        version_row = insert_draft_proc_def_version(
            tenant_id=tenant_id,
            proc_def_id=dmn_id,
            version=new_version,
            definition=merged_definition,
            snapshot=xml_snapshot,
            message=f"피드백 기반 DMN 규칙 제안({owner_label}): {decision_name}",
            parent_version=None,
            source_todolist_id=todo_ids[0] if todo_ids else None,
        )
        if not version_row:
            log(f"⚠️ DMN draft 버전 생성 실패: batch_id={batch_id}, dmn_id={dmn_id}")
            return {"applied": False, "error": "draft_version_failed", "owner": owner_label}

        description = f"피드백 기반 자동 DMN 규칙 제안입니다({owner_label}). draft version={new_version}"

        pr_row = insert_dmn_merge_request(
            tenant_id=tenant_id,
            proc_def_id=dmn_id,
            version=new_version,
            title=f"[Feedback] {dmn_id} DMN 규칙 개선: {decision_name}",
            description=description,
            requester_ids=requester_ids,
            reviewer_id=approver_id,
        )
        if not pr_row:
            log(f"⚠️ DMN 병합 요청 생성 실패: batch_id={batch_id}, dmn_id={dmn_id}")
            return {
                "applied": False,
                "error": "merge_request_failed",
                "draft_version": new_version,
                "owner": owner_label,
            }

        log(
            f"✅ DMN draft 버전+병합요청 생성: batch_id={batch_id}, dmn_id={dmn_id}, "
            f"version={new_version}, pr_id={pr_row.get('id')}"
        )
        return {
            "applied": True,
            "dmn_id": dmn_id,
            "draft_version": new_version,
            "proc_def_version_id": version_row.get("uuid"),
            "resource_pull_request_id": pr_row.get("id"),
            "owner": owner_label,
        }

    results: List[Dict[str, Any]] = []
    applied_dmn_ids: set = set()

    approved_id = target.get("id")
    if approved_id:
        results.append(
            await _apply_for_owner(approved_id, f"승인된 대상: {target.get('name') or approved_id}")
        )
        applied_dmn_ids.add(approved_id)

    rows = await fetch_todolist_rows_by_ids(todo_ids)
    agents = await get_agents_info(_union_user_ids(rows), _union_assignees(rows))

    if not agents:
        if not results:
            log(f"담당 에이전트 없음 — 개선할 기존 DMN을 특정할 수 없어 건너뜀: batch_id={batch_id}")
        return results

    matching = await match_feedback_to_agents(_dmn_artifact_as_text(artifact), agents)
    agent_feedbacks = matching.get("agent_feedbacks", [])
    if not agent_feedbacks and not results:
        log(f"매칭된 DMN 담당 에이전트 없음: batch_id={batch_id}")
    for fb_item in agent_feedbacks:
        aid = fb_item.get("agent_id")
        aname = fb_item.get("agent_name", "Unknown")
        if not aid:
            continue
        candidates = _agent_dmn_candidates(tenant_id, aid)
        resolved = await resolve_dmn_identity(artifact, candidates)
        if resolved.get("decision") != "UPDATE" or not resolved.get("id"):
            continue
        if resolved["id"] in applied_dmn_ids:
            continue
        applied_dmn_ids.add(resolved["id"])
        results.append(await _apply_for_owner(resolved["id"], f"에이전트: {aname}"))

    return results


# ---------------------------------------------------------------------------
# 5. 승인된 PROCESS_DEFINITION target 실행 — draft proc_def_version +
#    resource_pull_requests 병합 요청만 만든다. 라이브 proc_def.definition은
#    건드리지 않는다(add-process-definition-apply).
# ---------------------------------------------------------------------------

async def apply_approved_process_definition_target(
    batch: Dict[str, Any],
    artifact: Dict[str, Any],
    approver_id: Optional[str] = None,
) -> Dict[str, Any]:
    """승인된 PROCESS_DEFINITION target을 draft proc_def_version 행 +
    resource_pull_requests 병합 요청으로 반영한다. apply_approved_dmn_target과
    동일한 패턴 — grounding LLM 호출이나 검증기 없이, artifact의 activities/
    sequences/gateways를 live definition 복사본에 기계적으로 병합할 뿐이다.
    라이브 proc_def는 쓰지 않는다: draft 생성과 PR 오픈까지가 이 함수의
    책임이다(add-process-definition-apply design.md Non-Goals).

    draft의 snapshot은 라이브 proc_def.bpmn XML이 있으면 그 XML에 같은 변경을
    반영해 채운다(merge_process_definition_artifact_into_xml) — 라이브 XML이
    없거나 병합에 실패하면 병합된 definition의 JSON 문자열로 폴백한다.

    병합 요청의 requester/reviewer 귀속은 apply_approved_dmn_target과 동일하다
    (fix-merge-request-requester).
    """
    batch_id = batch.get("id")
    tenant_id = batch.get("tenant_id", "")
    proc_def_id = batch.get("proc_def_id", "")

    live_definition = _get_proc_def_definition(tenant_id, proc_def_id)
    if not isinstance(live_definition, dict):
        log(f"⚠️ PROCESS_DEFINITION 적용 실패, proc_def를 찾을 수 없음: batch_id={batch_id}, proc_def_id={proc_def_id}")
        return {"applied": False, "error": "proc_def_not_found"}

    parent_version = live_definition.get("version")
    merged_definition, demoted_count = merge_process_definition_artifact_into_definition(
        live_definition, artifact
    )
    new_version = compute_next_draft_version(tenant_id, proc_def_id)

    live_bpmn_xml = _get_proc_def_bpmn_xml(tenant_id, proc_def_id)
    xml_snapshot = (
        merge_process_definition_artifact_into_xml(live_bpmn_xml, live_definition, merged_definition)
        if live_bpmn_xml
        else None
    )
    snapshot = xml_snapshot or json.dumps(merged_definition, ensure_ascii=False)

    summary = artifact.get("summary", "")
    collected_items = batch.get("collected_items") or []
    todo_ids = [item.get("todo_id") for item in collected_items if item.get("todo_id")]
    requester_ids = _feedback_author_ids(collected_items)

    version_row = insert_draft_proc_def_version(
        tenant_id=tenant_id,
        proc_def_id=proc_def_id,
        version=new_version,
        definition=merged_definition,
        snapshot=snapshot,
        message=f"피드백 기반 프로세스 흐름 변경 제안: {summary}",
        parent_version=parent_version,
        source_todolist_id=todo_ids[0] if todo_ids else None,
        version_tag="minor",
    )
    if not version_row:
        log(f"⚠️ PROCESS_DEFINITION draft 버전 생성 실패: batch_id={batch_id}")
        return {"applied": False, "error": "draft_version_failed"}

    description = f"피드백 기반 자동 프로세스 흐름 변경 제안입니다. draft version={new_version}"
    if demoted_count:
        description += f" (매칭되는 기존 요소가 없어 새 요소로 추가된 MODIFY 항목 {demoted_count}건 포함 — 리뷰 시 확인 필요)"

    pr_row = insert_bpmn_merge_request(
        tenant_id=tenant_id,
        proc_def_id=proc_def_id,
        version=new_version,
        title=f"[Feedback] {proc_def_id} 프로세스 흐름 개선: {summary}",
        description=description,
        requester_ids=requester_ids,
        reviewer_id=approver_id,
    )
    if not pr_row:
        log(f"⚠️ PROCESS_DEFINITION 병합 요청 생성 실패: batch_id={batch_id}")
        return {"applied": False, "error": "merge_request_failed", "draft_version": new_version}

    log(
        f"✅ PROCESS_DEFINITION draft 버전+병합요청 생성: batch_id={batch_id}, "
        f"version={new_version}, pr_id={pr_row.get('id')}, demoted_modify_count={demoted_count}"
    )
    return {
        "applied": True,
        "draft_version": new_version,
        "proc_def_version_id": version_row.get("uuid"),
        "resource_pull_request_id": pr_row.get("id"),
    }


# ---------------------------------------------------------------------------
# 6. target 적용 실행과 결과 기록 — 피드백 작성자가 "내 피드백이 무엇이 되었는지"를
#    볼 수 있도록 적용 상태·결과(병합 요청, draft 버전)를 그 target에 남긴다.
# ---------------------------------------------------------------------------

def _definition_apply_outcome(resource_type: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """DMN_RULE/PROCESS_DEFINITION 적용 함수의 반환값을 target 기록 모양으로 바꾼다."""
    apply_results = [
        {
            "resource_type": resource_type,
            "resource_id": r.get("dmn_id") or r.get("proc_def_id"),
            "draft_version": r.get("draft_version"),
            "proc_def_version_id": r.get("proc_def_version_id"),
            "pull_request_id": r.get("resource_pull_request_id"),
            "owner": r.get("owner"),
            "error": None if r.get("applied") else r.get("error"),
        }
        for r in results
    ]
    applied = [r for r in results if r.get("applied")]
    errors = [str(r.get("error")) for r in results if not r.get("applied") and r.get("error")]
    if applied:
        status = "APPLIED"
    elif errors:
        status = "FAILED"
    else:
        status = "NO_CHANGE"
    return {"apply_status": status, "apply_results": apply_results, "apply_error": "; ".join(errors) or None}


async def sync_batch_feedback_status(batch: Dict[str, Any]) -> None:
    """배치 target들의 결정·적용 상태로 워크아이템 feedback_status를 맞춘다.

    하나라도 반영됐으면 COMPLETED, 아직 대기·적용 중인 target이 있으면 건드리지 않고,
    모두 끝났는데 반영된 것이 없으면 실패가 있을 때 FAILED, 전부 반려면 REJECTED,
    그 외(승인됐지만 변경 없음)는 COMPLETED다.
    """
    targets = batch.get("targets") or []
    if not targets:
        return
    if any(t.get("apply_status") == "APPLIED" for t in targets):
        status = "COMPLETED"
    elif any((t.get("status") or "PENDING") == "PENDING" or t.get("apply_status") == "APPLYING" for t in targets):
        return
    elif any(t.get("apply_status") == "FAILED" for t in targets):
        status = "FAILED"
    elif all(t.get("status") == "REJECTED" for t in targets):
        status = "REJECTED"
    else:
        status = "COMPLETED"
    for item in batch.get("collected_items") or []:
        todo_id = item.get("todo_id")
        if todo_id:
            await update_feedback_status(todo_id, status)


async def run_target_apply(
    batch: Dict[str, Any],
    target_index: int,
    approver_id: Optional[str] = None,
    approver_name: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> Dict[str, Any]:
    """승인된 target 하나를 적용하고 그 결과를 target에 기록한다.

    auth_token은 승인자의 Bearer 토큰이다. 스킬 API가 요청자 JWT로 테넌트를 검증하므로
    SKILL 적용(스킬 조회·커밋)은 승인자 권한으로 나간다 — 토큰이 없으면 401로 실패한다.

    적용 전 apply_status=APPLYING을 먼저 남겨, 오래 걸리는 SKILL(Deep Agent) 적용 중에도
    화면이 "반영 중"을 보여줄 수 있게 한다. 적용 중 예외가 나도 FAILED로 남긴다 —
    APPLYING에 멈춰 있으면 작성자는 영원히 "반영 중"만 보게 된다.
    """
    batch_id = batch["id"]
    target = (batch.get("targets") or [])[target_index]
    ttype = target.get("type")
    use_auth_token(auth_token)
    patch_target(batch_id, target_index, {"apply_status": "APPLYING", "apply_started_at": datetime.now(timezone.utc).isoformat()})

    try:
        if ttype == "SKILL":
            outcome = await apply_approved_proposal(
                batch, target.get("artifact", ""), target.get("name"),
                approver_id=approver_id, approver_name=approver_name,
            )
        elif ttype == "DMN_RULE":
            results = await apply_approved_dmn_target(batch, target, approver_id=approver_id)
            outcome = _definition_apply_outcome("dmn", results)
        elif ttype == "PROCESS_DEFINITION":
            result = await apply_approved_process_definition_target(
                batch, target.get("artifact") or {}, approver_id=approver_id
            )
            outcome = _definition_apply_outcome("bpmn", [{**result, "proc_def_id": batch.get("proc_def_id")}])
        else:
            outcome = {"apply_status": "FAILED", "apply_results": [], "apply_error": f"알 수 없는 target: {ttype}"}
    except Exception as e:
        handle_error(f"target적용({ttype})", e)
        outcome = {"apply_status": "FAILED", "apply_results": [], "apply_error": str(e)[:500]}

    outcome["applied_at"] = datetime.now(timezone.utc).isoformat()
    updated = patch_target(batch_id, target_index, outcome)
    await sync_batch_feedback_status(updated or batch)
    log(f"target 적용 기록: batch_id={batch_id}, index={target_index}, type={ttype}, status={outcome['apply_status']}")
    return outcome
