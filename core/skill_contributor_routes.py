"""
스킬별 사람 기여 이력 조회 API (스펙 agent-feedback_skill-contribution-tracking).

스킬 생성/수정(core/learning_committers/skill_committer.py) 및 피드백 제안 승인
(core/feedback_batch_manager.py::apply_approved_proposal)에서 기록된
skill_contributions 이력을 스킬별로 집계해, 기여자와 상대적 기여 비중을 반환한다.
"""

from fastapi import APIRouter, HTTPException

from core.database import fetch_skill_contributors
from core.skill_api_client import check_skill_exists

router = APIRouter(prefix="/api/skills", tags=["skill-contributors"])


@router.get("/{skill_name}/contributors")
async def get_skill_contributors(skill_name: str, tenant_id: str = ""):
    if not check_skill_exists(skill_name, tenant_id or ""):
        raise HTTPException(status_code=404, detail=f"skill not found: {skill_name}")

    rows = fetch_skill_contributors(tenant_id, skill_name)

    buckets: dict = {}
    total = 0
    for row in rows:
        uid = row.get("contributor_user_id")
        if not uid:
            continue
        bucket = buckets.setdefault(
            uid, {"contributor_user_id": uid, "contributor_name": row.get("contributor_name"), "count": 0}
        )
        bucket["count"] += 1
        if row.get("contributor_name"):
            bucket["contributor_name"] = row.get("contributor_name")
        total += 1

    contributors = [
        {**bucket, "share": round(bucket["count"] / total, 4) if total else 0.0}
        for bucket in buckets.values()
    ]
    contributors.sort(key=lambda c: (-c["count"], c["contributor_user_id"]))

    return {"skill_name": skill_name, "contributors": contributors, "total_contributions": total}
