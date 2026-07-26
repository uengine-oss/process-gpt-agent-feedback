"""
1회성 백필: 과거에 승인된 SKILL target의 결정 이력(feedback_proposals.targets[].decided_by)과
그 배치의 원 피드백 작성자(collected_items[].user_id)를 skill_contributions 에 채운다.

agent-feedback_skill-contribution-tracking 도입 이전에 이미 승인되어 반영된 스킬은
기여 이력이 비어 있으므로, GET /api/skills/{id}/contributors 가 처음부터 완전한
목록을 돌려주도록 이 스크립트로 한 번 채운다.

멱등: (tenant_id, skill_name, contributor_user_id, source_proposal_id) 조합이 이미
있으면 건너뛴다 — 여러 번 실행해도 중복 기록되지 않는다.

실행:
    cd services/agent-feedback && uv run python -m scripts.backfill_skill_contributions
"""

from typing import Any, Dict, List

from core.database import get_db_client, initialize_db, record_skill_contribution
from utils.logger import log


def _union_user_ids(items: List[Dict[str, Any]]) -> List[str]:
    ids: List[str] = []
    seen = set()
    for item in items:
        uid = str(item.get("user_id") or "").strip()
        if uid and uid not in seen:
            seen.add(uid)
            ids.append(uid)
    return ids


def _already_recorded(existing: List[Dict[str, Any]], skill_name: str, contributor_user_id: str, proposal_id: str) -> bool:
    return any(
        r.get("skill_name") == skill_name
        and r.get("contributor_user_id") == contributor_user_id
        and r.get("source_proposal_id") == proposal_id
        for r in existing
    )


def backfill() -> Dict[str, int]:
    supabase = get_db_client()

    proposals_resp = supabase.table("feedback_proposals").select("*").execute()
    proposals = proposals_resp.data or []

    existing_resp = supabase.table("skill_contributions").select(
        "skill_name, contributor_user_id, source_proposal_id"
    ).execute()
    existing = existing_resp.data or []

    recorded = 0
    skipped = 0

    for proposal in proposals:
        proposal_id = proposal.get("id")
        tenant_id = proposal.get("tenant_id")
        targets = proposal.get("targets") or []
        collected_items = proposal.get("collected_items") or []
        author_ids = _union_user_ids(collected_items)

        for target in targets:
            if target.get("type") != "SKILL" or target.get("status") != "APPROVED":
                continue
            skill_name = target.get("name")
            if not skill_name or not tenant_id:
                continue

            contributor_ids = list(author_ids)
            approver_id = target.get("decided_by")
            approver_name = target.get("decided_by_name")
            if approver_id and approver_id not in contributor_ids:
                contributor_ids.append(approver_id)

            for uid in contributor_ids:
                if _already_recorded(existing, skill_name, uid, proposal_id):
                    skipped += 1
                    continue
                record_skill_contribution(
                    tenant_id=tenant_id,
                    skill_name=skill_name,
                    contributor_user_id=uid,
                    contribution_type="PROPOSAL_APPROVED",
                    contributor_name=approver_name if uid == approver_id else None,
                    source_proposal_id=proposal_id,
                )
                existing.append(
                    {"skill_name": skill_name, "contributor_user_id": uid, "source_proposal_id": proposal_id}
                )
                recorded += 1

    return {"proposals_scanned": len(proposals), "recorded": recorded, "skipped_existing": skipped}


if __name__ == "__main__":
    initialize_db()
    result = backfill()
    log(f"스킬 기여 이력 백필 완료: {result}")
