"""
스킬 피드백 제안(제안함) 조회/승인/반려 API.

제안(proposal)은 트리거된 배치가 분류된 결과로, target(SKILL/DMN_RULE/PROCESS_DEFINITION)
하나 이상을 가질 수 있고 각 target은 독립적인 결정 상태(PENDING/APPROVED/REJECTED)를 가진다.
승인/반려는 target 단위로 이뤄지며, 한 target을 결정해도 다른 target에는 영향을 주지 않는다.

승인된 SKILL target의 실제 스킬 개선 실행(feedback_batch_manager.apply_approved_proposal)은
Deep Agent 실행까지 포함해 오래 걸릴 수 있으므로, 승인 응답은 target 결정 반영까지만 동기로
처리하고 실제 실행은 백그라운드 태스크로 넘긴다.

DMN_RULE/PROCESS_DEFINITION target 승인은 각각 draft proc_def_version +
resource_pull_requests 병합 요청을 만든다 — 라이브 proc_def.definition은 쓰지 않는다.
DMN_RULE은 배치 워크아이템의 담당 에이전트별로 팬아웃하며 그 판단에 LLM 호출(에이전트
매칭 + 기존 DMN 식별)이 필요해졌으므로 SKILL과 동일하게 백그라운드로 넘긴다.
PROCESS_DEFINITION은 여전히 LLM 호출 없이 DB 쓰기 몇 번이면 끝나므로 백그라운드로 넘기지
않고 응답에 결과를 바로 담는다 (openspec/changes/add-feedback-proposal-apply,
openspec/changes/add-process-definition-apply design.md 참고).
"""

import asyncio
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

from core.database import (
    fetch_batch_by_id,
    fetch_batches_for_proc_defs,
    fetch_feedback_rows_assigned_to,
    fetch_feedback_rows_by_proc_inst_ids,
    fetch_participating_proc_inst_ids,
    fetch_proc_def_names,
    fetch_proc_inst_summaries,
    fetch_proposed_batches,
    fetch_resource_pull_requests_by_ids,
    fetch_todolist_rows_with_feedback_by_user,
    fetch_user_briefs,
    mark_target_decision,
)
from core.feedback_batch_manager import (
    BATCH_TRIGGER_COUNT,
    BATCH_TRIGGER_MAX_AGE,
    batch_workitem_count,
    run_target_apply,
    sync_batch_feedback_status,
)

router = APIRouter(prefix="/feedback-proposals", tags=["feedback-proposals"])

_VALID_TARGET_TYPES = {"SKILL", "DMN_RULE", "PROCESS_DEFINITION"}


class DecisionBody(BaseModel):
    approver_id: Optional[str] = None
    approver_name: Optional[str] = None
    approver_email: Optional[str] = None
    decision_note: Optional[str] = None
    # 화면이 누른 target의 배열 위치. 주면 그 target만 결정한다(같은 type이 여럿일 때 오결정 방지).
    target_index: Optional[int] = None


def _find_decided_index(
    before: Dict[str, Any], after: Dict[str, Any], target_type: str
) -> Optional[int]:
    """type만으로는 target을 유일하게 특정할 수 없다 — 같은 type(예: DMN_RULE)의
    target이 여러 개일 수 있어서다(classify_and_extract_proposal이 서로 다른 관심사를
    같은 type으로 여러 개 낼 수 있음). 결정 RPC는 호출당 PENDING 하나만 결정하므로,
    결정 전/후 targets 배열을 같은 인덱스로 비교해 이번 호출로 실제 PENDING을 벗어난
    target의 위치를 찾는다 — 배열 순서는 RPC가 인덱스 자리에서만 갱신하므로(jsonb_set)
    유지된다.
    """
    before_targets = before.get("targets") or []
    after_targets = after.get("targets") or []
    for i, (before_t, after_t) in enumerate(zip(before_targets, after_targets)):
        if (
            after_t.get("type") == target_type
            and (before_t.get("status") or "PENDING") == "PENDING"
            and after_t.get("status") != "PENDING"
        ):
            return i
    return None


def _serialize_proposal(b: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": b["id"],
        "tenant_id": b.get("tenant_id"),
        "proc_def_id": b.get("proc_def_id"),
        "activity_id": b.get("activity_id"),
        "targets": b.get("targets") or [],
        "candidate_skill_names": b.get("candidate_skill_names") or [],
        "collected_item_count": len(b.get("collected_items") or []),
        "first_collected_at": b.get("first_collected_at"),
        "proposed_at": b.get("proposed_at"),
    }


@router.get("")
async def list_feedback_proposals(tenant_id: str = ""):
    batches = await fetch_proposed_batches(tenant_id)
    proposals = [_serialize_proposal(b) for b in batches]
    return {"proposals": proposals, "total": len(proposals)}


# ---------------------------------------------------------------------------
# 내 피드백 처리 현황 — 피드백 작성자가 자기 피드백이 어디까지 갔는지 본다
# (process-gpt-vue3 openspec/changes/feedback-processing-status/planning.md)
# ---------------------------------------------------------------------------

# 카드 전체 상태를 고를 때의 우선순위 — 앞설수록 "더 나아간" 결과다.
_STAGE_PRIORITY = [
    "MERGED", "APPLIED", "APPLYING", "PENDING_REVIEW", "APPROVED", "APPLY_FAILED", "NO_CHANGE", "REJECTED",
]
_LEGACY_EMPTY_PROPOSAL_REASON = "분류 결과가 남지 않은 이전 방식의 제안이라 더 진행되지 않습니다"


def _artifact_summary(target: Dict[str, Any]) -> str:
    artifact = target.get("artifact")
    if isinstance(artifact, str):
        return artifact
    if not isinstance(artifact, dict):
        return ""
    if target.get("type") == "DMN_RULE":
        decision = artifact.get("decision") or {}
        lines = [decision.get("name") or ""]
        for rule in artifact.get("rules") or []:
            if isinstance(rule, dict):
                lines.append(f"{rule.get('when', '')} → {rule.get('then', '')}")
        return "\n".join(l for l in lines if l)
    return artifact.get("summary") or ""


def _target_stage(target: Dict[str, Any], pull_requests: Dict[str, Dict[str, Any]]) -> str:
    status = target.get("status") or "PENDING"
    if status == "PENDING":
        return "PENDING_REVIEW"
    if status == "REJECTED":
        return "REJECTED"
    apply_status = target.get("apply_status")
    if apply_status == "APPLIED":
        prs = [pull_requests.get(r.get("pull_request_id")) for r in target.get("apply_results") or []]
        if any(pr and pr.get("status") == "MERGED" for pr in prs):
            return "MERGED"
        return "APPLIED"
    return {"APPLYING": "APPLYING", "FAILED": "APPLY_FAILED", "NO_CHANGE": "NO_CHANGE"}.get(apply_status, "APPROVED")


def _serialize_target(target: Dict[str, Any], pull_requests: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "type": target.get("type"),
        "id": target.get("id"),
        "name": target.get("name"),
        "status": target.get("status") or "PENDING",
        "stage": _target_stage(target, pull_requests),
        "artifact_summary": _artifact_summary(target),
        "decided_by_name": target.get("decided_by_name"),
        "decided_at": target.get("decided_at"),
        "decision_note": target.get("decision_note"),
        "apply_status": target.get("apply_status"),
        "apply_error": target.get("apply_error"),
        "applied_at": target.get("applied_at"),
        "apply_results": [
            {**r, "pull_request": pull_requests.get(r.get("pull_request_id"))}
            for r in target.get("apply_results") or []
        ],
    }


def _batch_summary(batch: Dict[str, Any]) -> Dict[str, Any]:
    first = batch.get("first_collected_at")
    deadline = None
    if first:
        try:
            first_dt = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
            deadline = (first_dt + BATCH_TRIGGER_MAX_AGE).isoformat()
        except ValueError:
            deadline = None
    discard_reason = batch.get("discard_reason")
    if batch.get("status") in ("PROPOSED", "RESOLVED") and not batch.get("targets"):
        discard_reason = _LEGACY_EMPTY_PROPOSAL_REASON
    return {
        "id": batch["id"],
        "status": batch.get("status"),
        "collected_item_count": len(batch.get("collected_items") or []),
        # 트리거는 워크아이템 수로 센다(is_batch_triggered) — 화면도 이 값을 보여준다.
        "workitem_count": batch_workitem_count(batch.get("collected_items") or []),
        "trigger_count": BATCH_TRIGGER_COUNT,
        "first_collected_at": first,
        "trigger_deadline": deadline,
        "proposed_at": batch.get("proposed_at"),
        "discard_reason": discard_reason,
        "dropped_targets": [
            {"type": t.get("type"), "artifact_summary": _artifact_summary(t), "drop_reason": t.get("drop_reason")}
            for t in batch.get("dropped_targets") or []
        ],
    }


def _round_stage(batch: Dict[str, Any], targets: List[Dict[str, Any]]) -> str:
    """한 처리 회차(배치)의 단계. target이 여럿이면 가장 앞선 결과를 쓴다."""
    status = batch.get("status")
    if status == "COLLECTING":
        return "COLLECTING"
    if status == "CLASSIFYING":
        return "CLASSIFYING"
    if status == "DISCARDED" or not targets:
        return "DISCARDED"
    stages = {t["stage"] for t in targets}
    for stage in _STAGE_PRIORITY:
        if stage in stages:
            return stage
    return "PENDING_REVIEW"


def _pending_stage(row: Dict[str, Any]) -> str:
    """아직 어떤 배치에도 들어가지 않은 피드백의 단계."""
    if not row.get("proc_def_id"):
        # 프로세스에 속하지 않은 작업은 수집 RPC가 고르지 않는다.
        return "NOT_ELIGIBLE"
    if row.get("status") != "DONE":
        # 완료된 워크아이템만 수집한다 — 완료되면 그때까지 남긴 피드백이 한 단위로 수집된다.
        return "WAITING_DONE"
    return "WAITING"


def _find_batch_for_item(
    batches_by_todo: Dict[str, List[Dict[str, Any]]], todo_id: str, item: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """배치 항목은 (todo_id, user_id, time, content)를 그대로 담는다 — 작성자와 시각으로 먼저
    짝짓고, 시각이 없는 옛 항목은 내용으로 짝짓는다."""
    user_id = str(item.get("user_id") or "")
    time = str(item.get("time") or "").strip()
    content = item.get("content") or ""
    for batch in batches_by_todo.get(todo_id, []):
        for c in batch.get("collected_items") or []:
            if str(c.get("todo_id")) != todo_id or str(c.get("user_id") or "") != user_id:
                continue
            if (time and c.get("time") == time) or (not time and c.get("content") == content):
                return batch
    return None


_SCOPES = {"participating", "mine"}


def _collect_scope_rows(tenant_id: str, user_id: str, scope: str) -> List[Dict[str, Any]]:
    """화면에 보일 워크아이템(피드백이 있는 todolist 행)을 모은다.

    - mine: 내가 피드백을 남긴 워크아이템
    - participating: 위에 더해, 내가 참여자(bpm_proc_inst.participants)인 인스턴스의 워크아이템과
      내가 담당자인 워크아이템 — 내가 관여한 업무에 누가 어떤 피드백을 남겼고 그것이 어떻게
      처리됐는지 함께 본다.
    """
    rows_by_id: Dict[str, Dict[str, Any]] = {}
    sources = [fetch_todolist_rows_with_feedback_by_user(tenant_id, user_id)]
    if scope == "participating":
        sources.append(fetch_feedback_rows_by_proc_inst_ids(tenant_id, fetch_participating_proc_inst_ids(tenant_id, user_id)))
        sources.append(fetch_feedback_rows_assigned_to(tenant_id, user_id))
    for rows in sources:
        for row in rows:
            rows_by_id.setdefault(str(row["id"]), row)
    return list(rows_by_id.values())


@router.get("/my-feedback")
async def list_my_feedback(tenant_id: str, user_id: str, scope: str = "participating"):
    """워크아이템마다, 그 워크아이템에 남겨진 피드백이 어떤 처리 회차로 묶여 어떻게 처리됐는지.

    처리 단위는 워크아이템이다. 한 워크아이템의 피드백은 수집될 때 한 단위로 배치에 들어가고,
    그 배치가 처리(분류·승인·반영)되는 것이 한 회차다. 처리된 뒤에 같은 워크아이템에 남긴
    피드백은 다음 회차로 묶인다. 회차에는 누가 남겼든 그 워크아이템의 피드백이 모두 들어간다 —
    scope는 어떤 워크아이템을 보여줄지만 정한다(_collect_scope_rows).
    """
    if scope not in _SCOPES:
        raise HTTPException(status_code=400, detail=f"unknown scope: {scope}")

    rows = _collect_scope_rows(tenant_id, user_id, scope)
    batches = fetch_batches_for_proc_defs(tenant_id, [r.get("proc_def_id") for r in rows])

    batches_by_todo: Dict[str, List[Dict[str, Any]]] = {}
    for batch in batches:
        for todo_id in {str(c.get("todo_id")) for c in batch.get("collected_items") or [] if c.get("todo_id")}:
            batches_by_todo.setdefault(todo_id, []).append(batch)

    pr_ids = [
        r.get("pull_request_id")
        for b in batches for t in b.get("targets") or [] for r in t.get("apply_results") or []
    ]
    pull_requests = fetch_resource_pull_requests_by_ids(pr_ids)
    proc_def_names = fetch_proc_def_names(tenant_id, [r.get("proc_def_id") for r in rows])
    instances = fetch_proc_inst_summaries(tenant_id, [r.get("proc_inst_id") for r in rows])
    authors = fetch_user_briefs(
        tenant_id,
        [str(f.get("user_id") or "") for r in rows for f in r.get("feedback") or [] if isinstance(f, dict)],
    )

    def _entry(fb: Dict[str, Any]) -> Dict[str, Any]:
        uid = str(fb.get("user_id") or "")
        author = authors.get(uid) or {}
        return {
            "content": fb.get("content") or "",
            "time": fb.get("time"),
            "user_id": uid or None,
            "author_name": author.get("name") or None,
            "author_profile": author.get("profile"),
            "is_mine": uid == user_id,
        }

    workitems = []
    for row in rows:
        feedback = row.get("feedback")
        if not isinstance(feedback, list):
            continue
        todo_id = str(row["id"])
        # 수집은 시각 순으로 feedback_collected_count개까지 진행된다(extract_new_feedback_items).
        ordered = sorted((f for f in feedback if isinstance(f, dict)), key=lambda f: f.get("time", ""))
        collected_count = row.get("feedback_collected_count") or 0

        rounds_by_batch: Dict[str, Dict[str, Any]] = {}
        pending: List[Dict[str, Any]] = []
        untracked: List[Dict[str, Any]] = []
        for position, fb in enumerate(ordered):
            entry = _entry(fb)
            batch = _find_batch_for_item(batches_by_todo, todo_id, fb)
            if batch:
                rounds_by_batch.setdefault(batch["id"], {"batch": batch, "feedbacks": []})["feedbacks"].append(entry)
            elif position < collected_count:
                untracked.append(entry)
            else:
                pending.append(entry)

        rounds = []
        for r in sorted(rounds_by_batch.values(), key=lambda r: str(r["batch"].get("first_collected_at") or "")):
            targets = [_serialize_target(t, pull_requests) for t in r["batch"].get("targets") or []]
            rounds.append({
                "stage": _round_stage(r["batch"], targets),
                "batch": _batch_summary(r["batch"]),
                "targets": targets,
                "feedbacks": r["feedbacks"],
            })
        if untracked:
            # 배치 기록이 생기기 전(이전 방식)에 처리된 피드백 — 맨 앞 회차로 둔다.
            rounds.insert(0, {"stage": "UNTRACKED", "batch": None, "targets": [], "feedbacks": untracked})
        for n, r in enumerate(rounds, start=1):
            r["round"] = n

        entries = [f for r in rounds for f in r["feedbacks"]] + pending
        instance = instances.get(row.get("proc_inst_id")) or {}
        workitems.append({
            "todo_id": todo_id,
            "proc_def_id": row.get("proc_def_id"),
            "proc_def_name": proc_def_names.get(row.get("proc_def_id")) or row.get("proc_def_id"),
            "activity_id": row.get("activity_id"),
            "activity_name": row.get("activity_name") or row.get("activity_id"),
            "proc_inst_id": row.get("proc_inst_id"),
            "proc_inst_name": instance.get("name"),
            "proc_inst_status": instance.get("status"),
            "workitem_status": row.get("status"),
            "feedback_count": len(entries),
            "my_feedback_count": sum(1 for f in entries if f["is_mine"]),
            "author_count": len({f["user_id"] for f in entries if f["user_id"]}),
            "last_feedback_at": max((f["time"] for f in entries if f["time"]), default=None),
            "stage": rounds[-1]["stage"] if rounds else _pending_stage(row),
            "rounds": rounds,
            "pending": {"stage": _pending_stage(row), "feedbacks": pending} if pending else None,
        })

    workitems.sort(key=lambda w: w.get("last_feedback_at") or "", reverse=True)
    return {"workitems": workitems, "total": len(workitems)}


@router.get("/{proposal_id}")
async def get_feedback_proposal(proposal_id: str):
    batch = fetch_batch_by_id(proposal_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")
    return _serialize_proposal(batch)


@router.post("/{proposal_id}/targets/{target_type}/approve")
async def approve_feedback_proposal_target(
    proposal_id: str, target_type: str, body: DecisionBody, authorization: Optional[str] = Header(default=None)
):
    if target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown target_type: {target_type}")
    if not body.approver_id:
        raise HTTPException(status_code=400, detail="approver_id is required")

    batch = fetch_batch_by_id(proposal_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")

    updated = await mark_target_decision(
        proposal_id,
        target_type=target_type,
        status="APPROVED",
        decided_by=body.approver_id,
        decided_by_name=body.approver_name,
        decided_by_email=body.approver_email,
        decision_note=body.decision_note,
        target_index=body.target_index,
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail=(
                f"target '{target_type}' is not pending on this proposal "
                "(already decided, not found, or proposal not PROPOSED)"
            ),
        )

    # 스킬 API는 요청자 JWT로 테넌트를 검증한다 — 승인자의 토큰을 적용 단계까지 넘긴다.
    auth_token = (authorization or "")[7:] if (authorization or "").lower().startswith("bearer ") else None
    target_index = body.target_index if body.target_index is not None else _find_decided_index(batch, updated, target_type)
    if target_index is None:
        raise HTTPException(status_code=500, detail="decided target could not be located")

    if target_type in ("SKILL", "DMN_RULE"):
        # SKILL(Deep Agent)과 DMN_RULE(에이전트 팬아웃 매칭)은 LLM 호출이 있어 오래 걸린다 —
        # 승인 응답은 결정 반영까지만 하고 적용은 백그라운드로 넘긴다. 진행·결과는
        # target.apply_status/apply_results로 남는다.
        asyncio.create_task(
            run_target_apply(
                updated, target_index,
                approver_id=body.approver_id, approver_name=body.approver_name, auth_token=auth_token,
            )
        )
        return {"approved": True, "id": proposal_id, "target": target_type, "target_index": target_index, "applied": True}

    # PROCESS_DEFINITION: draft 버전 생성 + 병합 요청 오픈은 LLM 호출 없이 DB 쓰기 몇
    # 번이면 끝나므로 응답에 결과를 바로 담는다.
    outcome = await run_target_apply(
        updated, target_index, approver_id=body.approver_id, approver_name=body.approver_name, auth_token=auth_token
    )
    first = (outcome.get("apply_results") or [{}])[0]
    return {
        "approved": True,
        "id": proposal_id,
        "target": target_type,
        "target_index": target_index,
        "applied": outcome.get("apply_status") == "APPLIED",
        "draft_version": first.get("draft_version"),
        "resource_pull_request_id": first.get("pull_request_id"),
    }


@router.post("/{proposal_id}/targets/{target_type}/reject")
async def reject_feedback_proposal_target(proposal_id: str, target_type: str, body: DecisionBody):
    if target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown target_type: {target_type}")

    batch = fetch_batch_by_id(proposal_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")

    updated = await mark_target_decision(
        proposal_id,
        target_type=target_type,
        status="REJECTED",
        decided_by=body.approver_id,
        decided_by_name=body.approver_name,
        decided_by_email=body.approver_email,
        decision_note=body.decision_note,
        target_index=body.target_index,
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail=(
                f"target '{target_type}' is not pending on this proposal "
                "(already decided, not found, or proposal not PROPOSED)"
            ),
        )

    # 모든 target이 결정됐으면 워크아이템 feedback_status를 결과에 맞춘다(전부 반려면 REJECTED).
    await sync_batch_feedback_status(updated)

    return {"rejected": True, "id": proposal_id, "target": target_type}
