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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional

from core.database import fetch_batch_by_id, fetch_proposed_batches, mark_target_decision, update_feedback_status
from core.feedback_batch_manager import (
    apply_approved_proposal,
    apply_approved_dmn_target,
    apply_approved_process_definition_target,
)

router = APIRouter(prefix="/feedback-proposals", tags=["feedback-proposals"])

_VALID_TARGET_TYPES = {"SKILL", "DMN_RULE", "PROCESS_DEFINITION"}


class DecisionBody(BaseModel):
    approver_id: Optional[str] = None
    approver_name: Optional[str] = None
    approver_email: Optional[str] = None
    decision_note: Optional[str] = None


def _find_target(batch: Dict[str, Any], target_type: str) -> Optional[Dict[str, Any]]:
    for t in batch.get("targets") or []:
        if t.get("type") == target_type:
            return t
    return None


def _find_decided_target(
    before: Dict[str, Any], after: Dict[str, Any], target_type: str
) -> Optional[Dict[str, Any]]:
    """type만으로는 target을 유일하게 특정할 수 없다 — 같은 type(예: DMN_RULE)의
    target이 여러 개일 수 있어서다(classify_and_extract_proposal이 서로 다른 관심사를
    같은 type으로 여러 개 낼 수 있음). decide_feedback_proposal_target RPC는 호출당
    PENDING 하나만 결정하므로, 결정 전/후 targets 배열을 같은 인덱스로 비교해 이번
    호출로 실제 PENDING을 벗어난 target을 찾는다 — 배열 순서는 RPC가 인덱스 자리에서만
    갱신하므로(jsonb_set) 유지된다.
    """
    before_targets = before.get("targets") or []
    after_targets = after.get("targets") or []
    for before_t, after_t in zip(before_targets, after_targets):
        if (
            after_t.get("type") == target_type
            and (before_t.get("status") or "PENDING") == "PENDING"
            and after_t.get("status") != "PENDING"
        ):
            return after_t
    return _find_target(after, target_type)


def _all_targets_decided(batch: Dict[str, Any]) -> bool:
    targets = batch.get("targets") or []
    return bool(targets) and all((t.get("status") or "PENDING") != "PENDING" for t in targets)


def _any_target_approved(batch: Dict[str, Any]) -> bool:
    return any(t.get("status") == "APPROVED" for t in batch.get("targets") or [])


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


@router.get("/{proposal_id}")
async def get_feedback_proposal(proposal_id: str):
    batch = fetch_batch_by_id(proposal_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")
    return _serialize_proposal(batch)


@router.post("/{proposal_id}/targets/{target_type}/approve")
async def approve_feedback_proposal_target(proposal_id: str, target_type: str, body: DecisionBody):
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
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail=(
                f"target '{target_type}' is not pending on this proposal "
                "(already decided, not found, or proposal not PROPOSED)"
            ),
        )

    if target_type == "SKILL":
        skill_target = _find_decided_target(batch, updated, "SKILL") or {}
        extracted_rule = skill_target.get("artifact", "")
        bound_skill_name = skill_target.get("name")
        asyncio.create_task(apply_approved_proposal(updated, extracted_rule, bound_skill_name))
        return {"approved": True, "id": proposal_id, "target": target_type, "applied": True}

    if target_type == "DMN_RULE":
        # 에이전트 매칭 + 기존 DMN 식별에 LLM 호출이 필요해 SKILL과 동일하게 백그라운드로
        # 넘긴다 — 승인 응답은 target 결정 반영까지만 동기로 처리한다.
        dmn_target = _find_decided_target(batch, updated, "DMN_RULE") or {}
        artifact = dmn_target.get("artifact") or {}
        asyncio.create_task(
            apply_approved_dmn_target(
                updated,
                artifact,
                approver_id=body.approver_id,
                approver_name=body.approver_name,
            )
        )
        return {"approved": True, "id": proposal_id, "target": target_type, "applied": True}

    # PROCESS_DEFINITION: draft 버전 생성 + 병합 요청 오픈은 LLM 호출 없이 DB 쓰기 몇
    # 번이면 끝나므로 DMN_RULE과 동일하게 백그라운드로 넘기지 않고 응답에 결과를 바로 담는다.
    process_definition_target = _find_decided_target(batch, updated, "PROCESS_DEFINITION") or {}
    artifact = process_definition_target.get("artifact") or {}
    result = await apply_approved_process_definition_target(
        updated,
        artifact,
        approver_id=body.approver_id,
        approver_name=body.approver_name,
    )
    return {
        "approved": True,
        "id": proposal_id,
        "target": target_type,
        "applied": bool(result.get("applied")),
        "draft_version": result.get("draft_version"),
        "resource_pull_request_id": result.get("resource_pull_request_id"),
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
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail=(
                f"target '{target_type}' is not pending on this proposal "
                "(already decided, not found, or proposal not PROPOSED)"
            ),
        )

    # 모든 target이 결정됐고 그중 하나도 승인되지 않았다면(=전부 거절) 배치 전체를 종료 처리한다
    # — 단일 target 시절의 "거절 시 배치 종료" 동작과 동일. 이미 승인된 target이 있으면
    # (예: SKILL은 승인, 다른 target은 거절) 그 target의 승인 결과가 이미 workitem 상태를
    # 처리했으므로 여기서 건드리지 않는다.
    if _all_targets_decided(updated) and not _any_target_approved(updated):
        for item in updated.get("collected_items") or []:
            todo_id = item.get("todo_id")
            if todo_id:
                await update_feedback_status(todo_id, "REJECTED")

    return {"rejected": True, "id": proposal_id, "target": target_type}
