"""
피드백 배치 수집/트리거/제안 승인 실행 매니저.

두 개의 독립된 루프로 구성된다:
  - start_feedback_batch_collection: agent_feedback_task RPC로 피드백이 있는
    todolist 항목을 가져와 (tenant_id, proc_def_id, activity_id) 배치에 적재만 한다.
    스킬 조회나 스킬 개선은 하지 않는다.
  - start_feedback_batch_trigger: COLLECTING 배치들을 주기적으로 확인해 트리거
    조건(건수 또는 경과 시간)을 충족하면 일반 규칙을 추출해 제안(PROPOSED)으로
    전환하거나, 공통 규칙이 없으면 배치를 폐기(DISCARDED)한다.

승인된 제안을 실제 스킬 개선 파이프라인(process_feedback_with_deep_agent, 기존
HTTP 기반 skill_api_client 경로)에 태우는 apply_approved_proposal은 API 호출로
발생하는 이벤트이므로 폴링 루프가 아니라 여기서 직접 처리한다.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from utils.logger import log, handle_error
from core.database import (
    append_feedback_to_batch,
    fetch_collecting_batches,
    fetch_feedback_task,
    fetch_todolist_rows_by_ids,
    load_activity_skills,
    mark_batch_discarded,
    mark_batch_proposed,
    update_feedback_status,
    _get_agent_by_id,
)
from core.feedback_processor import extract_general_rule
from core.polling_manager import get_agents_info
from core.deep_agent import process_feedback_with_deep_agent

# 배치 트리거 임계값 — 5건 또는 3일 중 먼저 오는 조건 (튜닝 가능한 전역 상수로 시작)
BATCH_TRIGGER_COUNT = 5
BATCH_TRIGGER_MAX_AGE = timedelta(days=3)

# 배치 전체 이벤트 로그 상한 (여러 워크아이템의 이벤트를 합치므로 최신순으로 상한을 둔다)
_MAX_EVENTS_PER_BATCH = 100


def _extract_latest_feedback(feedback_raw: Any) -> tuple:
    """최신 피드백의 (content, user_id, time)을 반환한다."""
    if isinstance(feedback_raw, list):
        try:
            if feedback_raw:
                sorted_fb = sorted(feedback_raw, key=lambda x: x.get("time", ""), reverse=True)
                latest = sorted_fb[0]
                if isinstance(latest, dict):
                    return (
                        latest.get("content", ""),
                        str(latest.get("user_id") or "").strip(),
                        str(latest.get("time") or "").strip(),
                    )
                return (str(latest), "", "")
        except Exception:
            return ("", "", "")
    elif isinstance(feedback_raw, str):
        return (feedback_raw, "", "")
    elif feedback_raw:
        return (str(feedback_raw), "", "")
    return ("", "", "")


# ---------------------------------------------------------------------------
# 1. 수집 루프 — 피드백을 즉시 처리하지 않고 배치에 적재만 한다
# ---------------------------------------------------------------------------

async def process_feedback_collection_task(row: Dict[str, Any]) -> None:
    """피드백이 있는 워크아이템 1건을 해당 (tenant_id, proc_def_id, activity_id) 배치에 적재한다.

    스킬 탐색·스킬 개선은 하지 않는다 — 그건 배치가 트리거되어 사용자가 제안을
    승인한 뒤 apply_approved_proposal()이 담당한다.
    """
    todo_id = row["id"]
    feedback, feedback_user_id, feedback_time = _extract_latest_feedback(row.get("feedback", ""))
    tenant_id = str(row.get("tenant_id") or "").strip()
    proc_def_id = str(row.get("proc_def_id") or "").strip()
    activity_id = str(row.get("activity_id") or "").strip()

    if not feedback:
        log(f"⚠️ 피드백 내용 없음, 건너뜀: todo_id={todo_id}")
        await update_feedback_status(todo_id, "FAILED")
        return

    try:
        batch = await append_feedback_to_batch(
            tenant_id=tenant_id,
            proc_def_id=proc_def_id,
            activity_id=activity_id,
            todo_id=todo_id,
            content=feedback,
            time=feedback_time,
            user_id=feedback_user_id,
        )
        if not batch:
            log(f"⚠️ 배치 적재 실패: todo_id={todo_id}")
            await update_feedback_status(todo_id, "FAILED")
            return

        await update_feedback_status(todo_id, "COLLECTED")
        log(f"✅ 피드백 수집 완료: todo_id={todo_id}, batch_id={batch.get('id')}")
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

def is_batch_triggered(collected_items: List[Dict[str, Any]], first_collected_at: str) -> bool:
    """건수(5건) 또는 경과 시간(3일) 중 먼저 오는 조건을 충족했는지 확인한다.

    DB/LLM 호출 없이 순수하게 판단하므로 단위 테스트가 쉽다.
    """
    if len(collected_items) >= BATCH_TRIGGER_COUNT:
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


async def compute_candidate_skill_names(batch: Dict[str, Any]) -> List[str]:
    """제안 화면에 표시할 참고용 후보 스킬 목록을 계산한다 (읽기 전용, 부작용 없음).

    활동에 설정된 스킬 + 배치 워크아이템 담당 에이전트들의 기존 스킬을 합친 값이다.
    승인 후 Deep Agent가 실제로 다른 결정을 내릴 수 있으므로(예: 후보가 비어 있으면
    새 스킬을 생성) 이 값은 힌트일 뿐 확정 정보가 아니다.
    """
    items = batch.get("collected_items") or []
    todo_ids = [item.get("todo_id") for item in items if item.get("todo_id")]
    rows = await fetch_todolist_rows_by_ids(todo_ids)

    agents = await get_agents_info(_union_user_ids(rows), _union_assignees(rows))

    activity_skills = load_activity_skills(
        tenant_id=batch.get("tenant_id", ""),
        proc_def_id=batch.get("proc_def_id", ""),
        activity_id=batch.get("activity_id", ""),
    )

    names = set(activity_skills or [])
    for agent in agents:
        names.update(_parse_comma_skills(agent.get("skills")))

    return sorted(names)


async def _process_triggered_batch(batch: Dict[str, Any]) -> None:
    batch_id = batch["id"]
    items = batch.get("collected_items") or []

    general_rule = await extract_general_rule(items, task_description="")

    if not general_rule:
        if await mark_batch_discarded(batch_id):
            for item in items:
                todo_id = item.get("todo_id")
                if todo_id:
                    await update_feedback_status(todo_id, "REJECTED")
            log(f"배치 폐기(공통 규칙 없음): batch_id={batch_id}")
        return

    candidate_skill_names = await compute_candidate_skill_names(batch)
    if await mark_batch_proposed(batch_id, general_rule, candidate_skill_names):
        log(f"배치 제안 생성: batch_id={batch_id}, candidates={candidate_skill_names}")


async def start_feedback_batch_trigger(interval: int = 900) -> None:
    """COLLECTING 배치의 트리거 조건을 주기적으로 확인한다. 서버 수명 동안 무한 루프로 실행된다."""
    log(f"피드백 배치 트리거 확인 시작 (interval={interval}s)")

    while True:
        try:
            batches = await fetch_collecting_batches()
            for batch in batches:
                if is_batch_triggered(batch.get("collected_items") or [], batch.get("first_collected_at", "")):
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


async def apply_approved_proposal(batch: Dict[str, Any]) -> None:
    """승인된 배치를 기존 피드백→스킬 개선 파이프라인(process_feedback_with_deep_agent)에 태운다.

    호출 시점에 배치는 이미 status=APPROVED로 전환되어 있어야 한다(database.mark_batch_decided).
    이 함수는 그 실행 결과를 배치 내 각 todo_id의 feedback_status에만 반영한다.

    스킬 커밋은 process-gpt-agent-feedback이 원래 갖고 있던 HTTP API 경로(core/deep_agent.py
    + core/skill_tools.py)를 그대로 사용한다 — git PR 방식으로 새로 만들지 않는다.
    """
    batch_id = batch["id"]
    extracted_rule = batch.get("extracted_rule", "")
    items = batch.get("collected_items") or []
    todo_ids = [item.get("todo_id") for item in items if item.get("todo_id")]

    if not extracted_rule:
        log(f"⚠️ 승인된 배치에 extracted_rule이 없음: batch_id={batch_id}")
        for todo_id in todo_ids:
            await update_feedback_status(todo_id, "FAILED")
        return

    rows = await fetch_todolist_rows_by_ids(todo_ids)
    user_ids = _union_user_ids(rows)
    assignees = _union_assignees(rows)
    description = _representative_description(rows)

    from core.database import fetch_events_by_todo_id
    events: List[Dict[str, Any]] = []
    for todo_id in todo_ids:
        events.extend(await fetch_events_by_todo_id(todo_id))
    events.sort(key=lambda e: e.get("timestamp", ""))
    if len(events) > _MAX_EVENTS_PER_BATCH:
        events = events[-_MAX_EVENTS_PER_BATCH:]

    agents = await get_agents_info(user_ids, assignees)

    had_error = False

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
                )
                if result.get("error"):
                    had_error = True
                    log(f"⚠️ 에이전트 {aname} 처리 에러: {str(result['error'])[:200]}")
            except Exception as e:
                had_error = True
                log(f"⚠️ 에이전트 {aname} 피드백 처리 실패")
                handle_error(f"피드백처리({aname})", e)
    else:
        # 담당 에이전트가 없는 경우: 활동에 설정된 스킬이 있어도, 이 서비스의 Deep Agent
        # 파이프라인은 현재 agent_id 바인딩을 전제로 하므로 액티비티 전용 경로는
        # 아직 지원하지 않는다 (design.md Open Questions 참고 — 실제 발생 여부 확인 후 추가).
        log(f"에이전트 없음, 액티비티 전용 경로는 미지원: batch_id={batch_id}")
        had_error = True

    final_status = "FAILED" if had_error else "COMPLETED"
    for todo_id in todo_ids:
        await update_feedback_status(todo_id, final_status)

    log(f"승인된 배치 처리 완료: batch_id={batch_id}, status={final_status}")
