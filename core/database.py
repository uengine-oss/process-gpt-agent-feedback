import os
import socket
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from supabase import create_client, Client
from utils.logger import handle_error

# ============================================================================
# DB 설정 및 초기화
# ============================================================================

load_dotenv()
_db_client: Client | None = None

def initialize_db() -> None:
    """Supabase 클라이언트 초기화"""
    global _db_client
    if _db_client is not None:
        return
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL/KEY 설정 필요")
    _db_client = create_client(url, key)

def get_db_client() -> Client:
    """DB 클라이언트 반환"""
    if _db_client is None:
        raise RuntimeError("DB 클라이언트 비초기화: initialize_db() 먼저 호출하세요")
    return _db_client

# ============================================================================
# 피드백 작업 조회
# ============================================================================

async def fetch_feedback_task(limit: int = 1) -> Optional[Dict[str, Any]]:
    """DONE 상태이면서 feedback이 있는 작업 조회"""
    try:
        supabase = get_db_client()
        consumer_id = socket.gethostname()
        resp = supabase.rpc(
            'agent_feedback_task',
            {'p_limit': limit, 'p_consumer': consumer_id}
        ).execute()
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception as e:
        handle_error("피드백작업조회", e)

async def fetch_feedback_task_by_id(todo_id: str) -> Optional[Dict[str, Any]]:
    """특정 ID의 피드백 작업 조회 (테스트용)"""
    try:
        supabase = get_db_client()
        resp = (
            supabase
            .table('todolist')
            .select('*')
            .eq('id', todo_id)
            .single()
            .execute()
        )
        return resp.data if resp.data else None
    except Exception as e:
        handle_error("특정피드백작업조회", e)
        return None
    

# ============================================================================
# 에이전트 정보 조회
# ============================================================================
def _get_agent_by_id(agent_id: str) -> Optional[Dict[str, Any]]:
    """ID로 에이전트 조회"""
    supabase = get_db_client()
    resp = supabase.table('users').select(
        'id, username, role, goal, persona, tools, profile, is_agent, model, tenant_id'
    ).eq('id', agent_id).execute()
    if resp.data and resp.data[0].get('is_agent'):
        agent = resp.data[0]
        return {
            'id': agent.get('id'),
            'name': agent.get('username'),
            'role': agent.get('role'),
            'goal': agent.get('goal'),
            'persona': agent.get('persona'),
            'tools': agent.get('tools'),
            'profile': agent.get('profile'),
            'model': agent.get('model'),
            'tenant_id': agent.get('tenant_id')
        }
    return None