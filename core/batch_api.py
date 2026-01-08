"""
배치 중복 제거 API 엔드포인트
"""

from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from utils.logger import log, handle_error
from core.batch_deduplicator import BatchDeduplicator
from core.database import get_all_agents
from core.batch_rollback import rollback_batch_job, get_batch_job_history

router = APIRouter(prefix="/batch", tags=["batch"])


@router.get("/deduplicate")
async def batch_deduplicate(
    agent_id: Optional[str] = Query(None, description="특정 에이전트만 처리 (선택적)"),
    dry_run: bool = Query(True, description="DRY_RUN 모드 (기본값: true, 실제 실행 안 함)")
):
    """
    배치 중복 제거 실행 (수동 실행용 API)

    - agent_id가 있으면 해당 에이전트에 대해 **단일 배치 작업** 실행
    - agent_id가 없으면 **모든 에이전트에 대해 에이전트별 배치 작업을 순차 실행**
    - dry_run=true면 분석만 수행하고 실제 삭제는 하지 않음
    """
    try:
        log(f"🌐 배치 중복 제거 API 호출: agent_id={agent_id}, dry_run={dry_run}")
        
        deduplicator = BatchDeduplicator()

        # 단일 에이전트 배치
        if agent_id:
            result = await deduplicator.execute_batch_deduplication(
                agent_id=agent_id,
                dry_run=dry_run
            )

            if result.get("success"):
                return {
                    "success": True,
                    "dry_run": dry_run,
                    "message": "배치 중복 제거 완료 (단일 에이전트)",
                    **result
                }
            else:
                raise HTTPException(
                    status_code=500,
                    detail=result.get("error", "배치 중복 제거 실패")
                )

        # 모든 에이전트에 대해 에이전트별 배치 실행
        agents = get_all_agents()
        if not agents:
            return {
                "success": True,
                "dry_run": dry_run,
                "message": "처리할 에이전트가 없음",
                "total_agents": 0,
                "processed_agents": 0,
                "total_deleted": 0,
                "total_moved": 0,
                "total_kept": 0,
                "total_errors": 0,
                "results": []
            }

        all_results = []
        total_deleted = 0
        total_moved = 0
        total_kept = 0
        total_errors = 0

        for agent in agents:
            aid = agent.get("id")
            if not aid:
                continue

            try:
                res = await deduplicator.execute_batch_deduplication(
                    agent_id=aid,
                    dry_run=dry_run
                )
                all_results.append(res)

                if res.get("success"):
                    total_deleted += res.get("total_deleted", 0)
                    total_moved += res.get("total_moved", 0)
                    total_kept += res.get("total_kept", 0)
                    total_errors += res.get("total_errors", 0)
            except Exception as e:
                handle_error(f"배치중복제거API_에이전트_{aid}", e)
                all_results.append({
                    "success": False,
                    "agent_id": aid,
                    "error": str(e)
                })
                total_errors += 1

        return {
            "success": True,
            "dry_run": dry_run,
            "message": "배치 중복 제거 완료 (모든 에이전트, 에이전트별 배치)",
            "total_agents": len(agents),
            "processed_agents": sum(r.get("processed_agents", 0) for r in all_results if r.get("success")),
            "total_deleted": total_deleted,
            "total_moved": total_moved,
            "total_kept": total_kept,
            "total_errors": total_errors,
            "results": all_results
        }
            
    except Exception as e:
        error_msg = f"배치 중복 제거 API 실행 실패: {e}"
        log(f"❌ {error_msg}")
        handle_error("배치중복제거API", e)
        raise HTTPException(status_code=500, detail=error_msg)


@router.post("/rollback/{job_id}")
async def batch_rollback(job_id: str):
    """
    배치 작업 롤백
    
    - job_id: 롤백할 배치 작업 ID
    - 삭제/이동된 지식을 복구합니다
    """
    try:
        log(f"🌐 배치 작업 롤백 API 호출: job_id={job_id}")
        
        result = await rollback_batch_job(job_id)
        
        if result.get("success"):
            return {
                "success": True,
                "message": "배치 작업 롤백 완료",
                **result
            }
        else:
            raise HTTPException(
                status_code=400,
                detail=result.get("error", "배치 작업 롤백 실패")
            )
            
    except Exception as e:
        error_msg = f"배치 작업 롤백 API 실행 실패: {e}"
        log(f"❌ {error_msg}")
        handle_error("배치롤백API", e)
        raise HTTPException(status_code=500, detail=error_msg)


@router.get("/history")
async def batch_history(
    job_id: Optional[str] = Query(None, description="특정 작업 ID (선택적)"),
    limit: int = Query(50, description="최대 결과 수")
):
    """
    배치 작업 이력 조회
    
    - job_id가 없으면 최근 작업 목록 반환
    - job_id가 있으면 특정 작업 상세 정보 반환
    """
    try:
        history = await get_batch_job_history(job_id, limit)
        return {
            "success": True,
            "count": len(history),
            "history": history
        }
    except Exception as e:
        error_msg = f"배치 작업 이력 조회 실패: {e}"
        log(f"❌ {error_msg}")
        handle_error("배치작업이력조회API", e)
        raise HTTPException(status_code=500, detail=error_msg)

