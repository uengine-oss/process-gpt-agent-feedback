"""
스킬 피드백 제안(제안함) 조회/승인/반려 API.

승인된 제안의 실제 스킬 개선 실행(feedback_batch_manager.apply_approved_proposal)은
Deep Agent 실행까지 포함해 오래 걸릴 수 있으므로, 승인 응답은 배치 상태 전환까지만
동기로 처리하고 실제 실행은 백그라운드 태스크로 넘긴다.
"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from core.database import fetch_batch_by_id, fetch_proposed_batches, mark_batch_decided, update_feedback_status
from core.feedback_batch_manager import apply_approved_proposal

router = APIRouter(prefix="/feedback-proposals", tags=["feedback-proposals"])


class DecisionBody(BaseModel):
    approver_id: Optional[str] = None
    approver_name: Optional[str] = None
    approver_email: Optional[str] = None
    decision_note: Optional[str] = None


@router.get("")
async def list_feedback_proposals(tenant_id: str = ""):
    batches = await fetch_proposed_batches(tenant_id)
    proposals = [
        {
            "id": b["id"],
            "tenant_id": b.get("tenant_id"),
            "proc_def_id": b.get("proc_def_id"),
            "activity_id": b.get("activity_id"),
            "extracted_rule": b.get("extracted_rule"),
            "candidate_skill_names": b.get("candidate_skill_names") or [],
            "collected_item_count": len(b.get("collected_items") or []),
            "first_collected_at": b.get("first_collected_at"),
            "proposed_at": b.get("proposed_at"),
        }
        for b in batches
    ]
    return {"proposals": proposals, "total": len(proposals)}


@router.post("/{proposal_id}/approve")
async def approve_feedback_proposal(proposal_id: str, body: DecisionBody):
    if not body.approver_id:
        raise HTTPException(status_code=400, detail="approver_id is required")

    batch = fetch_batch_by_id(proposal_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")

    ok = await mark_batch_decided(
        proposal_id,
        status="APPROVED",
        decided_by=body.approver_id,
        decided_by_name=body.approver_name,
        decided_by_email=body.approver_email,
    )
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="proposal is not in PROPOSED status (already decided or not ready)",
        )

    batch["status"] = "APPROVED"
    asyncio.create_task(apply_approved_proposal(batch))

    return {"approved": True, "id": proposal_id}


@router.post("/{proposal_id}/reject")
async def reject_feedback_proposal(proposal_id: str, body: DecisionBody):
    batch = fetch_batch_by_id(proposal_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")

    ok = await mark_batch_decided(
        proposal_id,
        status="REJECTED",
        decided_by=body.approver_id,
        decided_by_name=body.approver_name,
        decided_by_email=body.approver_email,
        decision_note=body.decision_note,
    )
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="proposal is not in PROPOSED status (already decided or not ready)",
        )

    for item in batch.get("collected_items") or []:
        todo_id = item.get("todo_id")
        if todo_id:
            await update_feedback_status(todo_id, "REJECTED")

    return {"rejected": True, "id": proposal_id}
