import os
import socket
import hashlib
import json
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from supabase import create_client, Client
from utils.logger import handle_error, log

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
        resp = supabase.rpc(
            'agent_feedback_task',
            {'p_limit': limit}
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
# 이벤트 로그 조회 (특정 TODO 기준)
# ============================================================================

async def fetch_events_by_todo_id(todo_id: str) -> List[Dict[str, Any]]:
    """
    특정 TODO(ID)와 연관된 이벤트 로그를 시간순으로 조회

    - events 테이블 스키마
      id, job_id, todo_id, proc_inst_id, event_type, status, crew_type, data(jsonb), timestamp
    - 피드백 처리 시, 해당 워크아이템(todo_id)의 실제 스킬 사용 이력을 LLM에 제공하기 위해 사용
    """
    try:
        supabase = get_db_client()
        resp = (
            supabase
            .table("events")
            .select("*")
            .eq("todo_id", todo_id)
            .order("timestamp", desc=False)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        handle_error("이벤트로그조회", e)
        return []


# ============================================================================
# 피드백 상태 업데이트
# ============================================================================

async def update_feedback_status(todo_id: str, status: str) -> bool:
    """
    피드백 작업의 상태를 업데이트
    
    Args:
        todo_id: TODO ID
        status: 상태 값 (예: 'STARTED', 'COMPLETED', 'FAILED')
    
    Returns:
        업데이트 성공 여부
    """
    try:
        supabase = get_db_client()
        resp = (
            supabase
            .table('todolist')
            .update({'feedback_status': status})
            .eq('id', todo_id)
            .execute()
        )
        return True
    except Exception as e:
        handle_error("피드백상태업데이트", e)
        return False
    

# ============================================================================
# 에이전트 정보 조회
# ============================================================================
def _get_agent_by_id(agent_id: str) -> Optional[Dict[str, Any]]:
    """ID로 에이전트 조회"""
    supabase = get_db_client()
    resp = supabase.table('users').select('*').eq('id', agent_id).execute()
    if resp.data and resp.data[0].get('is_agent') and resp.data[0].get('agent_type') == 'agent':
        agent = resp.data[0]
        print('에이전트 이름: ', agent.get('username'))
        agent['name'] = agent['username']
        return agent
    return None

def get_all_agents() -> List[Dict[str, Any]]:
    """모든 에이전트 조회"""
    supabase = get_db_client()
    resp = (
        supabase.table('users')
        .select('*')
        .eq('is_agent', True)
        .eq('agent_type', 'agent')
        .execute()
    )
    agents = []
    if resp.data:
        for agent in resp.data:
            agent['name'] = agent.get('username', '')
            agents.append(agent)
    return agents


# ============================================================================
# 에이전트 초기 지식 셋팅 로그
# ============================================================================

async def fetch_agents_needing_setup(limit: int = 1) -> List[Dict[str, Any]]:
    """
    초기 지식 셋팅이 아직 되지 않은 에이전트 조회.
    users에서 is_agent=true, agent_type='agent', goal 있음 & agent_knowledge_setup_log에 없음.
    """
    try:
        supabase = get_db_client()
        resp = supabase.rpc('agent_needing_knowledge_setup', {'p_limit': limit}).execute()
        rows = resp.data or []
        agents = []
        for agent in rows:
            agent['name'] = agent.get('username', '')
            agents.append(agent)
        return agents
    except Exception as e:
        handle_error("에이전트셋팅대상조회", e)
        return []


def insert_agent_knowledge_setup_log(
    agent_id: str,
    tenant_id: Optional[str] = None,
    status: str = 'DONE'
) -> bool:
    """에이전트 초기 지식 셋팅 완료 로그 INSERT"""
    try:
        supabase = get_db_client()
        supabase.table('agent_knowledge_setup_log').insert({
            'agent_id': agent_id,
            'tenant_id': tenant_id,
            'status': status
        }).execute()
        return True
    except Exception as e:
        handle_error("에이전트셋팅로그기록", e)
        return False


# ============================================================================
# 스킬 동기화 (users / tenants 테이블)
# ============================================================================

def _parse_comma_separated_skills(skills_text: Optional[str]) -> List[str]:
    """콤마로 조인된 스킬 문자열을 리스트로 변환."""
    if not skills_text:
        return []
    return [s.strip() for s in skills_text.split(",") if s.strip()]


def _join_comma_separated_skills(skills_list: List[str]) -> str:
    """스킬 리스트를 콤마로 조인된 문자열로 변환."""
    return ",".join(sorted(set(skills_list)))


def update_agent_and_tenant_skills(agent_id: str, skill_name: str, operation: str) -> None:
    """
    Skill 생성/삭제 이후 users.skills (text)와 tenants.skills (text[])를 동기화.

    - users.skills: 스킬명을 콤마(,)로 조인한 문자열
    - tenants.skills: 스킬명 문자열 배열 (text[])
    """
    from utils.logger import log  # 순환 import 방지용 내부 import

    supabase = get_db_client()

    # 1) 에이전트 정보 조회 (tenant_id, 기존 skills 포함)
    resp = (
        supabase.table("users")
        .select("id, tenant_id, skills")
        .eq("id", agent_id)
        .single()
        .execute()
    )
    user = resp.data if resp.data else None
    if not user:
        log(f"에이전트를 찾을 수 없습니다 (users.skills 업데이트 생략): agent_id={agent_id}")
        return

    tenant_id = user.get("tenant_id")
    user_skills_text: Optional[str] = user.get("skills")
    user_skills = _parse_comma_separated_skills(user_skills_text)

    operation_upper = (operation or "").upper()

    # 2) users.skills 업데이트
    if operation_upper == "CREATE":
        if skill_name not in user_skills:
            user_skills.append(skill_name)
    elif operation_upper == "DELETE":
        user_skills = [s for s in user_skills if s != skill_name]

    new_user_skills_text = _join_comma_separated_skills(user_skills) if user_skills else None

    supabase.table("users").update(
        {"skills": new_user_skills_text}
    ).eq("id", agent_id).execute()
    log(f"users.skills 업데이트 완료: agent_id={agent_id}, skills={new_user_skills_text}")

    # 3) tenants.skills 업데이트 (tenant_id 기준)
    if not tenant_id:
        log(f"tenant_id가 없어 tenants.skills 업데이트를 건너뜁니다: agent_id={agent_id}")
        return

    tenant_resp = (
        supabase.table("tenants")
        .select("id, skills")
        .eq("id", tenant_id)
        .single()
        .execute()
    )
    tenant = tenant_resp.data if tenant_resp.data else None
    if not tenant:
        log(f"tenant를 찾을 수 없습니다 (tenants.skills 업데이트 생략): tenant_id={tenant_id}")
        return

    tenant_skills: Optional[list] = tenant.get("skills")  # text[] → Python list
    tenant_skills_list: List[str] = list(tenant_skills) if tenant_skills else []

    if operation_upper == "CREATE":
        if skill_name not in tenant_skills_list:
            tenant_skills_list.append(skill_name)
    elif operation_upper == "DELETE":
        tenant_skills_list = [s for s in tenant_skills_list if s != skill_name]

    supabase.table("tenants").update(
        {"skills": tenant_skills_list if tenant_skills_list else None}
    ).eq("id", tenant_id).execute()

    log(f"tenants.skills 업데이트 완료: tenant_id={tenant_id}, skills={tenant_skills_list}")

    # 4) agent_skills 테이블 동기화 (user_id, tenant_id, skill_name)
    if tenant_id:
        try:
            if operation_upper == "CREATE":
                supabase.table("agent_skills").upsert(
                    {"user_id": agent_id, "tenant_id": tenant_id, "skill_name": skill_name},
                    on_conflict="user_id,tenant_id,skill_name",
                ).execute()
                log(f"agent_skills INSERT 완료: agent_id={agent_id}, skill_name={skill_name}")
            elif operation_upper == "DELETE":
                (
                    supabase.table("agent_skills")
                    .delete()
                    .eq("user_id", agent_id)
                    .eq("tenant_id", tenant_id)
                    .eq("skill_name", skill_name)
                    .execute()
                )
                log(f"agent_skills DELETE 완료: agent_id={agent_id}, skill_name={skill_name}")
        except Exception as e:
            log(f"⚠️ agent_skills 동기화 실패 (무시하고 계속 진행): {e}")
            handle_error("agent_skills동기화", e)


# ============================================================================
# 에이전트 지식 변경 이력 기록 (통합)
# ============================================================================

def record_knowledge_history(
    knowledge_type: str,  # "MEMORY" | "DMN_RULE" | "SKILL"
    knowledge_id: str,  # MEMORY: memory_id, DMN_RULE: rule_id, SKILL: skill_name
    agent_id: str,
    tenant_id: Optional[str],
    operation: str,  # "CREATE" | "UPDATE" | "DELETE" | "MOVE"
    previous_content: Optional[Dict[str, Any]] = None,
    new_content: Optional[Dict[str, Any]] = None,
    feedback_content: Optional[str] = None,
    knowledge_name: Optional[str] = None,  # DMN_RULE: rule name, SKILL: skill name
    moved_from_storage: Optional[str] = None,  # MOVE인 경우
    moved_to_storage: Optional[str] = None,  # MOVE인 경우
    batch_job_id: Optional[str] = None,  # 배치 작업 ID
    version_uuid: Optional[str] = None  # DMN_RULE 버전 UUID (프론트엔드에서 버전 정보 조회용)
) -> Optional[str]:
    """
    에이전트 지식 변경 이력을 데이터베이스에 기록 (통합)
    
    Args:
        knowledge_type: 지식 타입 ("MEMORY" | "DMN_RULE" | "SKILL")
        knowledge_id: 지식 ID (MEMORY: memory_id, DMN_RULE: rule_id, SKILL: skill_name)
        agent_id: 에이전트 ID
        tenant_id: 테넌트 ID
        operation: 작업 타입 ("CREATE" | "UPDATE" | "DELETE" | "MOVE")
        previous_content: 이전 내용 (UPDATE/DELETE/MOVE 시)
        new_content: 새 내용 (CREATE/UPDATE/MOVE 시)
        feedback_content: 원본 피드백 내용 (선택적)
        knowledge_name: 지식 이름 (DMN_RULE: rule name, SKILL: skill name, MEMORY: None)
        moved_from_storage: 이동 전 저장소 (MOVE인 경우)
        moved_to_storage: 이동 후 저장소 (MOVE인 경우)
        batch_job_id: 배치 작업 ID (배치 작업으로 변경된 경우)
        version_uuid: DMN_RULE 버전 UUID (프론트엔드에서 버전 정보 조회용, DMN_RULE인 경우만)
    
    Returns:
        생성된 변경 이력의 UUID (version_uuid가 제공된 경우 해당 UUID 반환)
    """
    from utils.logger import log  # 순환 import 방지용 내부 import
    
    try:
        supabase = get_db_client()
        
        # Dict를 JSON 문자열로 직렬화 (TEXT 타입 저장을 위해)
        import json
        previous_content_str = None
        new_content_str = None
        
        if previous_content is not None:
            if isinstance(previous_content, dict):
                previous_content_str = json.dumps(previous_content, ensure_ascii=False)
            else:
                previous_content_str = str(previous_content)
        
        if new_content is not None:
            if isinstance(new_content, dict):
                new_content_str = json.dumps(new_content, ensure_ascii=False)
            else:
                new_content_str = str(new_content)
        
        # version_uuid가 제공된 경우 해당 UUID를 사용, 아니면 자동 생성
        history_id = version_uuid if version_uuid else None
        
        # version_uuid가 제공된 경우 해당 UUID를 사용, 아니면 자동 생성
        history_id = version_uuid if version_uuid else None
        
        record = {
            "knowledge_type": knowledge_type.upper(),
            "knowledge_id": knowledge_id,
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "operation": operation.upper(),
            "previous_content": previous_content_str,  # TEXT 타입으로 저장
            "new_content": new_content_str,  # TEXT 타입으로 저장
            "feedback_content": feedback_content,
            "knowledge_name": knowledge_name,
            "moved_from_storage": moved_from_storage,
            "moved_to_storage": moved_to_storage,
            "batch_job_id": batch_job_id
        }
        
        # None 값 제거 (데이터베이스에 NULL로 저장되도록)
        record = {k: v for k, v in record.items() if v is not None}
        
        # version_uuid가 제공된 경우 id로 지정하여 삽입 (변경 이력 UUID = 버전 UUID)
        if history_id:
            record["id"] = history_id
        
        resp = supabase.table("agent_knowledge_history").insert(record).execute()
        
        # 생성된 UUID 반환
        result_uuid = None
        if resp.data and len(resp.data) > 0:
            result_uuid = resp.data[0].get("id")
        elif history_id:
            result_uuid = history_id
        
        log(f"📝 지식 변경 이력 기록 완료: type={knowledge_type}, id={knowledge_id}, operation={operation}, history_uuid={result_uuid}")
        
        return result_uuid
        
    except Exception as e:
        # 변경 이력 기록 실패는 로그만 남기고 계속 진행 (작업 자체는 성공했을 수 있음)
        import traceback
        log(f"⚠️ 지식 변경 이력 기록 실패 (무시하고 계속 진행): {e}")
        log(f"   상세 에러: {traceback.format_exc()}")


# ============================================================================
# 에이전트 지식 레지스트리 관리
# ============================================================================

def _hash_content(content: str) -> str:
    """지식 내용의 해시 생성 (변경 감지용)"""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def register_knowledge(
    agent_id: str,
    tenant_id: Optional[str],
    knowledge_type: str,
    knowledge_id: str,
    knowledge_name: Optional[str] = None,
    content_summary: Optional[str] = None,
    content: Optional[str] = None
) -> bool:
    """
    에이전트 지식 레지스트리에 지식 등록/업데이트 (UPSERT)
    
    Args:
        agent_id: 에이전트 ID
        tenant_id: 테넌트 ID
        knowledge_type: 지식 타입
        knowledge_id: 지식 ID
        knowledge_name: 지식 이름
        content_summary: 지식 내용 요약
        content: 지식 전체 내용 (해시 생성용)
    
    Returns:
        저장 성공 여부
    """
    try:
        supabase = get_db_client()
        
        content_hash = None
        if content:
            content_hash = _hash_content(content)
        
        record = {
            'agent_id': agent_id,
            'tenant_id': tenant_id,
            'knowledge_type': knowledge_type.upper(),
            'knowledge_id': knowledge_id,
            'knowledge_name': knowledge_name,
            'content_summary': content_summary,
            'content_hash': content_hash
        }
        
        # None 값 제거
        record = {k: v for k, v in record.items() if v is not None}
        
        # UPSERT
        resp = (
            supabase
            .table('agent_knowledge_registry')
            .upsert(record, on_conflict='agent_id,knowledge_type,knowledge_id')
            .execute()
        )
        
        log(f"✅ 지식 레지스트리 등록: {knowledge_type}:{knowledge_id} (agent_id={agent_id})")
        return True
        
    except Exception as e:
        handle_error("지식레지스트리등록", e)
        return False


def unregister_knowledge(
    agent_id: str,
    knowledge_type: str,
    knowledge_id: str
) -> bool:
    """
    에이전트 지식 레지스트리에서 지식 제거
    
    Args:
        agent_id: 에이전트 ID
        knowledge_type: 지식 타입
        knowledge_id: 지식 ID
    
    Returns:
        삭제 성공 여부
    """
    try:
        supabase = get_db_client()
        
        (
            supabase
            .table('agent_knowledge_registry')
            .delete()
            .eq('agent_id', agent_id)
            .eq('knowledge_type', knowledge_type.upper())
            .eq('knowledge_id', knowledge_id)
            .execute()
        )
        
        log(f"🗑️ 지식 레지스트리 제거: {knowledge_type}:{knowledge_id} (agent_id={agent_id})")
        return True
        
    except Exception as e:
        handle_error("지식레지스트리제거", e)
        return False


def get_agent_knowledge_list(
    agent_id: str,
    knowledge_type: Optional[str] = None,
    limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    에이전트가 가진 모든 지식 목록 조회
    
    Args:
        agent_id: 에이전트 ID
        knowledge_type: 지식 타입 필터 (None이면 모든 타입)
        limit: 조회 제한 수 (None이면 제한 없음)
    
    Returns:
        지식 목록
    """
    try:
        supabase = get_db_client()
        
        query = (
            supabase
            .table('agent_knowledge_registry')
            .select('*')
            .eq('agent_id', agent_id)
            .order('updated_at', desc=True)
        )
        
        if knowledge_type:
            query = query.eq('knowledge_type', knowledge_type.upper())
        
        if limit:
            query = query.limit(limit)
        
        resp = query.execute()
        return resp.data if resp.data else []
        
    except Exception as e:
        handle_error("지식목록조회", e)
        return []


def check_knowledge_exists(
    agent_id: str,
    knowledge_type: str,
    knowledge_id: str
) -> bool:
    """
    특정 지식이 레지스트리에 존재하는지 확인
    
    Args:
        agent_id: 에이전트 ID
        knowledge_type: 지식 타입
        knowledge_id: 지식 ID
    
    Returns:
        존재 여부
    """
    try:
        supabase = get_db_client()
        
        resp = (
            supabase
            .table('agent_knowledge_registry')
            .select('id')
            .eq('agent_id', agent_id)
            .eq('knowledge_type', knowledge_type.upper())
            .eq('knowledge_id', knowledge_id)
            .limit(1)
            .execute()
        )
        
        return len(resp.data) > 0 if resp.data else False
        
    except Exception as e:
        handle_error("지식존재확인", e)
        return False


def update_knowledge_access_time(
    agent_id: str,
    knowledge_type: str,
    knowledge_id: str
) -> bool:
    """
    지식의 마지막 접근 시간 업데이트
    
    Args:
        agent_id: 에이전트 ID
        knowledge_type: 지식 타입
        knowledge_id: 지식 ID
    
    Returns:
        업데이트 성공 여부
    """
    try:
        supabase = get_db_client()
        
        (
            supabase
            .table('agent_knowledge_registry')
            .update({'last_accessed_at': 'now()'})
            .eq('agent_id', agent_id)
            .eq('knowledge_type', knowledge_type.upper())
            .eq('knowledge_id', knowledge_id)
            .execute()
        )
        
        return True
        
    except Exception as e:
        handle_error("접근시간업데이트", e)
        return False


