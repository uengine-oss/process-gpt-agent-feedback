"""
MEMORY 커밋 모듈
mem0에 저장하는 로직
"""

import os
from datetime import datetime
from typing import Optional
from mem0 import Memory
from utils.logger import log, handle_error
from dotenv import load_dotenv
from core.database import get_db_client, record_knowledge_history, _get_agent_by_id

load_dotenv()

# ============================================================================
# 설정 및 초기화
# ============================================================================

# 데이터베이스 연결 정보
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

if not all([DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME]):
    raise ValueError("❌ DB 연결 환경 변수가 설정되지 않았습니다. .env 파일을 확인해주세요.")

CONNECTION_STRING = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# ============================================================================
# Memory 커밋
# ============================================================================

def _get_memory_instance() -> Memory:
    """Supabase 기반 Memory 인스턴스 초기화"""
    config = {
        "vector_store": {
            "provider": "supabase",
            "config": {
                "connection_string": CONNECTION_STRING,
                "collection_name": "memories",
                "index_method": "hnsw",
                "index_measure": "cosine_distance"
            }
        }
    }
    return Memory.from_config(config_dict=config)


async def commit_to_memory(agent_id: str, content: str, source_type: str = "feedback", operation: str = "CREATE", memory_id: str = None) -> Optional[str]:
    """
    mem0에 CRUD 작업 수행
    
    Args:
        agent_id: 에이전트 ID
        content: 저장할 내용
        source_type: 메모리 타입 (information, feedback, guideline 등)
        operation: "CREATE" | "UPDATE" | "DELETE"
        memory_id: UPDATE/DELETE 시 기존 메모리 ID (필수)
    
    Raises:
        Exception: 작업 실패 시
    """
    try:
        memory = _get_memory_instance()
        
        if operation == "DELETE":
            if not memory_id:
                log(f"⚠️ DELETE 작업인데 memory_id가 없음")
                raise ValueError("DELETE 작업에는 memory_id가 필요합니다")
            
            # 삭제 전 이전 내용 조회 (변경 이력용)
            previous_content = None
            try:
                # mem0에서 메모리 조회 시도
                try:
                    memories = memory.get_all(agent_id=agent_id)
                    for mem in memories:
                        if mem.get("id") == memory_id:
                            previous_content = {
                                "memory": mem.get("memory", ""),
                                "metadata": mem.get("metadata", {})
                            }
                            break
                except Exception:
                    pass  # 조회 실패 시 무시
            except Exception:
                pass
            
            # PostgreSQL 함수를 사용하여 메모리 삭제
            try:
                supabase = get_db_client()
                supabase.rpc('delete_memory', {'mem_id': memory_id}).execute()
                log(f"🗑️ MEMORY 삭제 완료: 에이전트 {agent_id}, memory_id={memory_id}")
            except Exception as e:
                # fallback: mem0 API 사용
                log(f"⚠️ PostgreSQL 함수 삭제 실패, mem0 API로 재시도: {e}")
                memory.delete(memory_id, agent_id=agent_id)
                log(f"🗑️ MEMORY 삭제 완료 (fallback): 에이전트 {agent_id}, memory_id={memory_id}")
            
            # 변경 이력 기록 (실패 시 전체 작업 실패: "변경 이력에 저장 안되면 무조건 실패")
            try:
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else None
                
                # feedback_content에서 batch_job_id 추출 시도
                batch_job_id = None
                if "배치" in str(source_type) or "batch" in str(source_type).lower():
                    # 배치 작업으로 삭제된 경우 job_id 추출 (개선 가능)
                    pass
                
                record_knowledge_history(
                    knowledge_type="MEMORY",
                    knowledge_id=memory_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="DELETE",
                    previous_content=previous_content,
                    feedback_content=f"배치 작업: {source_type}" if "batch" in str(source_type).lower() else None,
                    batch_job_id=batch_job_id
                )
            except Exception as e:
                log(f"   ❌ MEMORY 변경 이력 기록 실패: {e}")
                raise
            
            return
        
        elif operation == "UPDATE":
            if not memory_id:
                log(f"⚠️ UPDATE 작업인데 memory_id가 없음")
                raise ValueError("UPDATE 작업에는 memory_id가 필요합니다")
            
            # 업데이트 전 이전 내용 조회 (변경 이력용)
            previous_content = None
            try:
                memories = memory.get_all(agent_id=agent_id)
                for mem in memories:
                    if mem.get("id") == memory_id:
                        previous_content = {
                            "memory": mem.get("memory", ""),
                            "metadata": mem.get("metadata", {})
                        }
                        break
            except Exception:
                pass
            
            metadata = {
                "type": source_type,
                "source": "user_feedback",
                "timestamp": datetime.now().isoformat(),
                "note": "This memory may be overridden by DMN or Skill"
            }
            
            # mem0에서 update 메서드 호출
            memory.update(memory_id, content, agent_id=agent_id, metadata=metadata)
            log(f"✏️ MEMORY 수정 완료: 에이전트 {agent_id}, memory_id={memory_id}, 타입={source_type}")
            
            # 변경 이력 기록 (실패 시 전체 작업 실패)
            try:
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else None
                
                new_content = {
                    "memory": content,
                    "metadata": metadata
                }
                
                record_knowledge_history(
                    knowledge_type="MEMORY",
                    knowledge_id=memory_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="UPDATE",
                    previous_content=previous_content,
                    new_content=new_content,
                    feedback_content=None
                )
            except Exception as e:
                log(f"   ❌ MEMORY 변경 이력 기록 실패: {e}")
                raise
            
            return
        
        else:  # CREATE
            metadata = {
                "type": source_type,
                "source": "user_feedback",
                "timestamp": datetime.now().isoformat(),
                "note": "This memory may be overridden by DMN or Skill"
            }
            
            result = memory.add(
                content,
                agent_id=agent_id,
                metadata=metadata,
                infer=False
            )
            
            # 생성된 메모리 ID 추출 (mem0 API 응답에서)
            memory_id = None
            if isinstance(result, dict):
                memory_id = result.get("id") or result.get("memory_id")
            elif isinstance(result, list) and result:
                memory_id = result[0].get("id") if isinstance(result[0], dict) else None
            
            log(f"✅ MEMORY 저장 완료: 에이전트 {agent_id}, 타입={source_type}, memory_id={memory_id}")
            
            # 변경 이력 기록 (memory_id가 있는 경우)
            if memory_id:
                try:
                    agent_info = _get_agent_by_id(agent_id)
                    tenant_id = agent_info.get("tenant_id") if agent_info else None
                    
                    new_content = {
                        "memory": content,
                        "metadata": metadata
                    }
                    
                    record_knowledge_history(
                        knowledge_type="MEMORY",
                        knowledge_id=memory_id,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        operation="CREATE",
                        new_content=new_content,
                        feedback_content=None
                    )
                except Exception as e:
                    log(f"   ❌ MEMORY 변경 이력 기록 실패: {e}")
                    raise
        
    except Exception as e:
        handle_error(f"MEMORY{operation}", e)
        raise
