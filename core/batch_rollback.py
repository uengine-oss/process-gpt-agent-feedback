"""
배치 작업 롤백 모듈
배치 작업으로 삭제/이동된 지식을 복구하는 기능
"""

import json
from typing import Dict, List, Optional
from datetime import datetime
from utils.logger import log, handle_error
from core.database import get_db_client
from core.learning_committers.memory_committer import commit_to_memory
from core.learning_committers.dmn_committer import commit_to_dmn_rule
from core.learning_committers.skill_committer import commit_to_skill


async def rollback_batch_job(job_id: str) -> Dict:
    """
    배치 작업 롤백 실행
    
    Args:
        job_id: 롤백할 배치 작업 ID
    
    Returns:
        롤백 결과
    """
    try:
        log(f"🔄 배치 작업 롤백 시작: job_id={job_id}")
        
        supabase = get_db_client()
        
        # 1. 배치 작업 이력 확인
        job_history = (
            supabase.table("batch_job_history")
            .select("*")
            .eq("job_id", job_id)
            .single()
            .execute()
        )
        
        if not job_history.data:
            raise ValueError(f"배치 작업을 찾을 수 없음: {job_id}")
        
        job_data = job_history.data
        
        # 이미 롤백된 경우 확인
        if job_data.get("status") == "ROLLED_BACK":
            log(f"⚠️ 이미 롤백된 배치 작업: {job_id}")
            return {
                "success": False,
                "error": "이미 롤백된 배치 작업입니다",
                "job_id": job_id
            }
        
        # DRY_RUN 모드인 경우 롤백 불가
        if job_data.get("dry_run"):
            log(f"⚠️ DRY_RUN 모드 배치 작업은 롤백할 수 없음: {job_id}")
            return {
                "success": False,
                "error": "DRY_RUN 모드 배치 작업은 롤백할 수 없습니다",
                "job_id": job_id
            }
        
        # 2. 백업 데이터 조회
        backups = (
            supabase.table("batch_job_backup")
            .select("*")
            .eq("job_id", job_id)
            .execute()
        )
        
        if not backups.data:
            log(f"⚠️ 백업 데이터가 없음: {job_id}")
            return {
                "success": False,
                "error": "백업 데이터가 없습니다",
                "job_id": job_id
            }
        
        # 3. 롤백 실행
        restored_count = 0
        errors = []
        
        for backup in backups.data:
            try:
                agent_id = backup.get("agent_id")
                storage_type = backup.get("storage_type")
                item_id = backup.get("item_id")
                operation = backup.get("operation")
                original_content = backup.get("original_content", {})
                
                if operation == "DELETE":
                    # 삭제된 항목 복구
                    await _restore_deleted_item(
                        agent_id=agent_id,
                        storage_type=storage_type,
                        item_id=item_id,
                        original_content=original_content
                    )
                    restored_count += 1
                    log(f"   ✅ {storage_type} 복구: id={item_id}")
                
                elif operation == "MOVE":
                    # 이동된 항목 롤백 (원본 복구 + 이동된 항목 삭제)
                    moved_to_storage = backup.get("moved_to_storage")
                    moved_to_id = backup.get("moved_to_id")
                    
                    # 1. 원본 복구
                    await _restore_deleted_item(
                        agent_id=agent_id,
                        storage_type=storage_type,
                        item_id=item_id,
                        original_content=original_content
                    )
                    
                    # 2. 이동된 항목 삭제
                    if moved_to_storage and moved_to_id:
                        await _delete_moved_item(
                            agent_id=agent_id,
                            storage_type=moved_to_storage,
                            item_id=moved_to_id
                        )
                    
                    restored_count += 1
                    log(f"   ✅ {storage_type} -> {moved_to_storage} 이동 롤백: id={item_id}")
                
            except Exception as e:
                error_msg = f"롤백 실패 ({storage_type}, id={item_id}): {e}"
                errors.append(error_msg)
                log(f"   ⚠️ {error_msg}")
                handle_error(f"배치롤백_{storage_type}", e)
        
        # 4. 배치 작업 상태 업데이트
        supabase.table("batch_job_history").update({
            "status": "ROLLED_BACK",
            "completed_at": datetime.now().isoformat()
        }).eq("job_id", job_id).execute()
        
        log(f"✅ 배치 작업 롤백 완료: job_id={job_id}, 복구={restored_count}, 에러={len(errors)}")
        
        return {
            "success": True,
            "job_id": job_id,
            "restored_count": restored_count,
            "errors": errors
        }
        
    except Exception as e:
        error_msg = f"배치 작업 롤백 실패: {e}"
        log(f"❌ {error_msg}")
        handle_error("배치롤백", e)
        return {
            "success": False,
            "error": error_msg,
            "job_id": job_id
        }


async def _restore_deleted_item(
    agent_id: str,
    storage_type: str,
    item_id: str,
    original_content: Dict
) -> None:
    """삭제된 항목 복구"""
    if storage_type == "MEMORY":
        content = original_content.get("memory") or original_content.get("content", "")
        await commit_to_memory(
            agent_id=agent_id,
            content=content,
            source_type="batch_rollback",
            operation="CREATE"
        )
    
    elif storage_type == "DMN_RULE":
        # DMN_RULE 복구: condition과 action 추출
        condition = original_content.get("condition", "")
        action = original_content.get("action", "")
        rule_name = original_content.get("name", f"복구된 규칙 {item_id[:8]}")
        
        if not condition or not action:
            # XML에서 추출 시도
            bpmn = original_content.get("bpmn", "")
            if bpmn:
                # 간단한 파싱 (개선 가능)
                condition = bpmn[:500]
                action = bpmn[500:1000] if len(bpmn) > 500 else bpmn
        
        await commit_to_dmn_rule(
            agent_id=agent_id,
            dmn_artifact={
                "condition": condition,
                "action": action,
                "name": rule_name
            },
            feedback_content="배치 작업 롤백",
            operation="CREATE"
        )
    
    elif storage_type == "SKILL":
        # SKILL 복구
        skill_name = original_content.get("name") or item_id
        description = original_content.get("description", "")
        steps = original_content.get("steps", [])
        
        if not steps:
            # content에서 steps 추출 시도
            content = original_content.get("content", "")
            if content:
                steps = [content]
        
        await commit_to_skill(
            agent_id=agent_id,
            skill_artifact={
                "name": skill_name,
                "description": description,
                "steps": steps,
                "overview": original_content.get("overview"),
                "usage": original_content.get("usage")
            },
            operation="CREATE",
            feedback_content="배치 작업 롤백"
        )


async def _delete_moved_item(
    agent_id: str,
    storage_type: str,
    item_id: str
) -> None:
    """이동된 항목 삭제 (롤백 시)"""
    if storage_type == "MEMORY":
        await commit_to_memory(
            agent_id=agent_id,
            content="",
            source_type="batch_rollback",
            operation="DELETE",
            memory_id=item_id
        )
    
    elif storage_type == "DMN_RULE":
        await commit_to_dmn_rule(
            agent_id=agent_id,
            dmn_artifact={},
            feedback_content="배치 작업 롤백",
            operation="DELETE",
            rule_id=item_id
        )
    
    elif storage_type == "SKILL":
        await commit_to_skill(
            agent_id=agent_id,
            skill_artifact={},
            operation="DELETE",
            skill_id=item_id,
            feedback_content="배치 작업 롤백"
        )


async def get_batch_job_history(job_id: Optional[str] = None, limit: int = 50) -> List[Dict]:
    """
    배치 작업 이력 조회
    
    Args:
        job_id: 특정 작업 ID (None이면 최근 작업 목록)
        limit: 최대 결과 수
    
    Returns:
        배치 작업 이력 목록
    """
    try:
        supabase = get_db_client()
        
        if job_id:
            result = (
                supabase.table("batch_job_history")
                .select("*")
                .eq("job_id", job_id)
                .single()
                .execute()
            )
            return [result.data] if result.data else []
        else:
            result = (
                supabase.table("batch_job_history")
                .select("*")
                .order("started_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
    
    except Exception as e:
        handle_error("배치작업이력조회", e)
        return []

