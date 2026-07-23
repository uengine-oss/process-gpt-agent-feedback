import os
import socket
import random
import string
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


def extract_new_feedback_items(feedback_raw: Any, collected_count: int) -> List[tuple]:
    """feedback 배열에서 아직 수집하지 않은(collected_count 이후의) 항목들을 시간순으로 반환.

    같은 워크아이템(todolist row)에 피드백이 여러 번 추가될 수 있는데, feedback_status만으로는
    "이미 처리한 배열 길이"를 알 수 없어 재처리 시 항상 최신 1건만 보고 나머지를 놓치는 문제가
    있었다. feedback_collected_count(이 워크아이템에서 지금까지 수집한 개수)를 기준으로,
    그 이후에 추가된 항목만 반환한다.

    Returns:
        (content, user_id, time) 튜플 리스트, time 오름차순
    """
    collected_count = max(0, collected_count or 0)

    if isinstance(feedback_raw, list):
        sorted_items = sorted(feedback_raw, key=lambda x: (x.get('time', '') if isinstance(x, dict) else ''))
        new_items = sorted_items[collected_count:]
        result = []
        for item in new_items:
            if isinstance(item, dict):
                result.append((
                    item.get('content', ''),
                    str(item.get('user_id') or '').strip(),
                    str(item.get('time') or '').strip(),
                ))
            elif item:
                result.append((str(item), '', ''))
        return result

    # 레거시: feedback이 배열이 아니라 단일 문자열인 경우 (한 번도 수집한 적 없을 때만 유효)
    if collected_count == 0 and feedback_raw:
        return [(str(feedback_raw), '', '')]
    return []


async def mark_feedback_collected_count(todo_id: str, count: int) -> bool:
    """이 워크아이템에서 지금까지 수집한 feedback 배열 항목 개수를 갱신.

    agent_feedback_task RPC가 "feedback 배열 길이 > feedback_collected_count"인 행도
    재조회 대상에 포함하므로, 이 값을 갱신해야 같은 항목을 중복 수집하지 않는다.
    """
    try:
        supabase = get_db_client()
        supabase.table('todolist').update({'feedback_collected_count': count}).eq('id', todo_id).execute()
        return True
    except Exception as e:
        handle_error("피드백수집개수갱신", e)
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
# 피드백 배치(feedback_proposals) — 수집/트리거/제안/승인·반려
# ============================================================================

async def fetch_todolist_rows_by_ids(todo_ids: List[str]) -> List[Dict[str, Any]]:
    """배치에 속한 todo_id들의 todolist row를 재조회 (승인 처리 시 담당자·설명 합성용)"""
    if not todo_ids:
        return []
    try:
        supabase = get_db_client()
        resp = supabase.table("todolist").select("*").in_("id", todo_ids).execute()
        return resp.data or []
    except Exception as e:
        handle_error("todolist재조회", e)
        return []


async def append_feedback_to_batch(
    tenant_id: str,
    proc_def_id: str,
    activity_id: str,
    todo_id: str,
    content: str,
    time: str,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """(tenant_id, proc_def_id, activity_id) 기준 COLLECTING 배치에 피드백을 원자적으로 적재.

    해당 배치가 없으면 새로 만든다 (append_feedback_to_batch DB 함수, 부분 유니크 인덱스 기반 upsert).
    """
    try:
        supabase = get_db_client()
        resp = supabase.rpc(
            "append_feedback_to_batch",
            {
                "p_tenant_id": tenant_id,
                "p_proc_def_id": proc_def_id,
                "p_activity_id": activity_id,
                "p_todo_id": todo_id,
                "p_content": content,
                "p_time": time,
                "p_user_id": user_id,
            },
        ).execute()
        data = resp.data
        if isinstance(data, list):
            return data[0] if data else None
        return data
    except Exception as e:
        handle_error("피드백배치적재", e)
        return None


async def fetch_collecting_batches(tenant_id: str = "") -> List[Dict[str, Any]]:
    """트리거 조건 확인 대상인 COLLECTING 배치 전체를 가져온다"""
    try:
        supabase = get_db_client()
        query = supabase.table("feedback_proposals").select("*").eq("status", "COLLECTING")
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        resp = query.execute()
        return resp.data or []
    except Exception as e:
        handle_error("COLLECTING배치조회", e)
        return []


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def mark_batch_proposed(
    batch_id: str,
    targets: List[Dict[str, Any]],
    candidate_skill_names: Optional[List[str]] = None,
) -> bool:
    """분류된 target(s)을 반영해 COLLECTING → PROPOSED로 전환 (중복 트리거 방지를 위해 COLLECTING인 것만).

    targets: [{"type": "SKILL"|"DMN_RULE"|"PROCESS_DEFINITION", "artifact": ..., "id": ..., "name": ...,
    "skill_name": ... (SKILL만)}, ...]
    id/name은 _fill_target_identity가 채운, 이 target이 가리키는 실제 기존 리소스의
    식별자다 — 클라이언트가 사이드바/상세 화면에 제안 배지를 매칭시키는 데 쓰므로
    반드시 그대로 보존해야 한다(이전에 여기서 빠뜨려 클라이언트에 전달되지 않던 버그
    수정). skill_name은 SKILL target에서 process-gpt-vue3의 기존 skill-proposal-indicator
    기능이 요구하는 매칭 키(id/name과 동일 값)이므로 함께 보존한다. 각 항목에
    status=PENDING과 빈 결정 필드를 채워 저장한다 — 결정은 target별로 독립적으로
    이뤄진다(core.database.mark_target_decision).
    """
    try:
        supabase = get_db_client()
        normalized_targets = [
            {
                "type": t.get("type"),
                "artifact": t.get("artifact"),
                "id": t.get("id"),
                "name": t.get("name"),
                "skill_name": t.get("skill_name"),
                "status": "PENDING",
                "decided_by": None,
                "decided_by_name": None,
                "decided_by_email": None,
                "decided_at": None,
                "decision_note": None,
            }
            for t in targets
        ]
        resp = (
            supabase.table("feedback_proposals")
            .update({
                "status": "PROPOSED",
                "targets": normalized_targets,
                "proposed_at": _now_iso(),
                "candidate_skill_names": candidate_skill_names or [],
            })
            .eq("id", batch_id)
            .eq("status", "COLLECTING")
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        handle_error("배치PROPOSED전환", e)
        return False


async def mark_batch_discarded(batch_id: str) -> bool:
    """공통 규칙 없음 판정 시 COLLECTING → DISCARDED로 전환"""
    try:
        supabase = get_db_client()
        resp = (
            supabase.table("feedback_proposals")
            .update({"status": "DISCARDED"})
            .eq("id", batch_id)
            .eq("status", "COLLECTING")
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        handle_error("배치DISCARDED전환", e)
        return False


async def fetch_proposed_batches(tenant_id: str = "") -> List[Dict[str, Any]]:
    """사용자 승인/반려 대기 중인 제안(PROPOSED) 목록을 가져온다"""
    try:
        supabase = get_db_client()
        query = supabase.table("feedback_proposals").select("*").eq("status", "PROPOSED")
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        resp = query.order("proposed_at", desc=True).execute()
        return resp.data or []
    except Exception as e:
        handle_error("PROPOSED배치조회", e)
        return []


def fetch_batch_by_id(batch_id: str) -> Optional[Dict[str, Any]]:
    try:
        supabase = get_db_client()
        resp = supabase.table("feedback_proposals").select("*").eq("id", batch_id).execute()
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception as e:
        handle_error("배치조회", e)
        return None


async def mark_target_decision(
    batch_id: str,
    target_type: str,
    status: str,
    decided_by: Optional[str] = None,
    decided_by_name: Optional[str] = None,
    decided_by_email: Optional[str] = None,
    decision_note: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """PROPOSED 제안 안의 특정 target 하나만 APPROVED/REJECTED로 결정한다 (다른 target에는 영향 없음).

    decide_feedback_proposal_target RPC(feedback_proposal_targets.sql)가 원자적으로:
    - 제안이 PROPOSED 상태가 아니면 아무 것도 하지 않고 NULL 반환
    - 대상 target_type이 없거나 이미 PENDING이 아니면(중복 결정) NULL 반환
    - 모든 target이 결정됐으면 제안 status를 RESOLVED로 전환

    반환값: 갱신된 제안 row(dict) 또는 결정 불가 시 None.

    주의: RPC가 "결정 불가"로 NULL을 반환해도, 함수 반환 타입이 테이블 row 타입(public.feedback_proposals)이라
    PostgREST/supabase-py를 거치면 진짜 NULL이 아니라 모든 필드가 None인 dict로 올 수 있다. 그런 dict는
    파이썬에서 비어있지 않아 truthy이므로, id(NOT NULL 기본키)가 실제로 채워져 있는지까지 확인해야
    "이미 결정된 target을 다시 승인" 같은 경우를 정상적으로 실패 처리할 수 있다.
    """
    try:
        supabase = get_db_client()
        resp = supabase.rpc(
            "decide_feedback_proposal_target",
            {
                "p_batch_id": batch_id,
                "p_target_type": target_type,
                "p_status": status,
                "p_decided_by": decided_by,
                "p_decided_by_name": decided_by_name,
                "p_decided_by_email": decided_by_email,
                "p_decision_note": decision_note,
            },
        ).execute()
        data = resp.data
        if isinstance(data, list):
            data = data[0] if data else None
        if not data or not data.get("id"):
            return None
        return data
    except Exception as e:
        handle_error("배치결정반영", e)
        return None


# ============================================================================
# 액티비티 설정 스킬 조회 (proc_def.definition 파싱, 담당 에이전트 없는 피드백용)
# ============================================================================

def _get_proc_def_definition(tenant_id: str, proc_def_id: str) -> Optional[Dict[str, Any]]:
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None
    try:
        supabase = get_db_client()
        resp = (
            supabase.table("proc_def")
            .select("definition")
            .eq("tenant_id", tid)
            .eq("id", pdid)
            .eq("isdeleted", False)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        return rows[0].get("definition")
    except Exception as e:
        handle_error("proc_def조회", e)
        return None


def _get_proc_def_bpmn_xml(tenant_id: str, proc_def_id: str) -> Optional[str]:
    """proc_def.bpmn 컬럼(있다면 라이브 프로세스의 실제 BPMN 2.0 XML)을 조회한다.

    PROCESS_DEFINITION target 적용 시 draft `proc_def_version.snapshot`을 JSON
    직렬화 대신 이 실제 XML에 병합하기 위해 쓴다(add-process-definition-apply
    design.md의 "확립된 BPMN XML 컨벤션 없음" 판단과 달리, 해당 proc_def_id의
    라이브 XML이 실제로 있으면 그걸 템플릿 삼아 병합할 수 있다 — 표준 태그를
    새로 지어내지 않는다). 라이브 XML이 없으면(컬럼 비어있음/행 없음) None을
    반환해 호출부가 JSON snapshot으로 폴백하게 한다.
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None
    try:
        supabase = get_db_client()
        resp = (
            supabase.table("proc_def")
            .select("bpmn")
            .eq("tenant_id", tid)
            .eq("id", pdid)
            .eq("isdeleted", False)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        bpmn = rows[0].get("bpmn")
        return bpmn if isinstance(bpmn, str) and bpmn.strip() else None
    except Exception as e:
        handle_error("proc_def_BPMN조회", e)
        return None


def fetch_proc_def_name(tenant_id: str, proc_def_id: str) -> Optional[str]:
    """proc_def.name 조회. PROCESS_DEFINITION/DMN_RULE target의 name 채우기에 쓴다.
    isdeleted=True인 행은 "존재하지 않음"으로 취급해 None을 반환한다."""
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None
    try:
        supabase = get_db_client()
        resp = (
            supabase.table("proc_def")
            .select("name")
            .eq("tenant_id", tid)
            .eq("id", pdid)
            .eq("isdeleted", False)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0].get("name") if rows else None
    except Exception as e:
        handle_error("proc_def이름조회", e)
        return None


def _get_dmn_definition_from_xml(tenant_id: str, proc_def_id: str) -> Optional[Dict[str, Any]]:
    """type='dmn' proc_def 행의 실제 규칙을 조회한다.

    이 타입의 행은 definition이 null인 게 정상 설계다 — 규칙은 bpmn 컬럼에 DMN 1.3
    XML로만 저장된다(process-gpt-deepagents의 get_process_detail과 동일 컨벤션).
    isdeleted=True거나 행이 없으면 "개선 대상이 더는 존재하지 않음" 신호로 None을
    반환한다 — 호출부가 draft/PR 생성을 건너뛰게 하기 위함이다. 행은 있는데 bpmn이
    비어 있으면(아직 규칙 없는 빈 DMN) 빈 규칙 dict를 반환한다.
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None
    try:
        supabase = get_db_client()
        resp = (
            supabase.table("proc_def")
            .select("bpmn")
            .eq("tenant_id", tid)
            .eq("id", pdid)
            .eq("type", "dmn")
            .eq("isdeleted", False)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        xml_text = rows[0].get("bpmn")
        from core.dmn_xml import xml_to_dmn_decisions_rules
        return xml_to_dmn_decisions_rules(xml_text or "")
    except Exception as e:
        handle_error("DMN XML조회", e)
        return None


def list_agent_dmn_rules(tenant_id: str, agent_id: str) -> List[Dict[str, Any]]:
    """특정 에이전트가 소유한 DMN 규칙 목록(proc_def.type='dmn', agent_id=agent_id) 조회.

    resolve_dmn_identity에 후보({"id","name"})로 그대로 넘길 수 있게 한다. proc_def에는
    description 컬럼이 없으므로 조회하지 않는다 — resolve_dmn_identity는 description이
    없으면 빈 문자열로 처리한다.
    """
    tid = (tenant_id or "").strip()
    aid = (agent_id or "").strip()
    if not (tid and aid):
        return []
    try:
        supabase = get_db_client()
        resp = (
            supabase.table("proc_def")
            .select("id,name")
            .eq("tenant_id", tid)
            .eq("agent_id", aid)
            .eq("type", "dmn")
            .eq("isdeleted", False)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        handle_error("에이전트DMN목록조회", e)
        return []


def load_activity_skills(tenant_id: str, proc_def_id: str, activity_id: str) -> List[str]:
    """프로세스 정의에서 특정 액티비티에 설정된 스킬 이름 목록을 반환. 실패 시 빈 리스트."""
    aid = (activity_id or "").strip()
    definition = _get_proc_def_definition(tenant_id, proc_def_id)
    if not aid or not isinstance(definition, dict):
        return []
    activities = definition.get("activities")
    if not isinstance(activities, list):
        return []

    target: Optional[Dict[str, Any]] = None
    for act in activities:
        if not isinstance(act, dict):
            continue
        for key in ("id", "activity_id", "activityId", "key"):
            if str(act.get(key) or "").strip() == aid:
                target = act
                break
        if target is not None:
            break
    if target is None:
        return []

    skills = target.get("skills")
    if skills is None:
        return []
    if isinstance(skills, str):
        return [s.strip() for s in skills.split(",") if s.strip()]
    if isinstance(skills, list):
        return [str(s).strip() for s in skills if str(s).strip()]
    return []


def update_activity_skills(tenant_id: str, proc_def_id: str, activity_id: str, skill_name: str, operation: str) -> bool:
    """load_activity_skills의 대칭 write. 담당 에이전트가 없는 배치에서 스킬을 생성/삭제했을 때
    users.skills 대신 proc_def.definition의 해당 활동 skills 목록에 반영한다.

    operation: "CREATE" (없으면 추가) | "DELETE" (있으면 제거). 실패 시 False.
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    aid = (activity_id or "").strip()
    name = (skill_name or "").strip()
    if not (tid and pdid and aid and name):
        return False

    try:
        supabase = get_db_client()
        resp = (
            supabase.table("proc_def")
            .select("definition")
            .eq("tenant_id", tid)
            .eq("id", pdid)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return False
        definition = rows[0].get("definition")
        if not isinstance(definition, dict):
            return False

        activities = definition.get("activities")
        if not isinstance(activities, list):
            return False

        target: Optional[Dict[str, Any]] = None
        for act in activities:
            if not isinstance(act, dict):
                continue
            for key in ("id", "activity_id", "activityId", "key"):
                if str(act.get(key) or "").strip() == aid:
                    target = act
                    break
            if target is not None:
                break
        if target is None:
            return False

        current = target.get("skills")
        if isinstance(current, str):
            skills_list = [s.strip() for s in current.split(",") if s.strip()]
        elif isinstance(current, list):
            skills_list = [str(s).strip() for s in current if str(s).strip()]
        else:
            skills_list = []

        op = (operation or "").upper()
        if op == "CREATE":
            if name not in skills_list:
                skills_list.append(name)
        elif op == "DELETE":
            skills_list = [s for s in skills_list if s != name]

        target["skills"] = skills_list

        supabase.table("proc_def").update({"definition": definition}).eq("tenant_id", tid).eq("id", pdid).execute()
        return True
    except Exception as e:
        handle_error("활동스킬갱신", e)
        return False


# ============================================================================
# DMN_RULE 승인 → proc_def_version draft + resource_pull_requests 병합 요청
# (add-feedback-proposal-apply)
# ============================================================================

def _generate_version_suffix(length: int = 11) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def compute_next_draft_version(tenant_id: str, proc_def_id: str) -> str:
    """proc_def_version에 새로 만들 draft 행의 version 문자열을 계산한다.

    실측된 라이브 컨벤션을 그대로 따른다: 해당 proc_def_id의 최신
    version_tag='major' 행의 version에 랜덤 접미사를 붙이고(예: "4.0" ->
    "4.0-fonqzvu4zbm"), major가 하나도 없으면 최신 version_tag='minor' 행으로
    대체한다. proc_def_version에 행이 아예 없으면 proc_def.definition.version을
    base로 쓴다(둘 다 없으면 "0.1").
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    base_version = "0.1"

    try:
        supabase = get_db_client()
        found = False
        for tag in ("major", "minor"):
            resp = (
                supabase.table("proc_def_version")
                .select("version")
                .eq("tenant_id", tid)
                .eq("proc_def_id", pdid)
                .eq("version_tag", tag)
                .order('"timeStamp"', desc=True)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            if rows:
                base_version = rows[0]["version"]
                found = True
                break

        if not found:
            definition = _get_proc_def_definition(tid, pdid)
            if isinstance(definition, dict) and definition.get("version"):
                base_version = str(definition["version"])
    except Exception as e:
        handle_error("버전계산", e)

    return f"{base_version}-{_generate_version_suffix()}"


def _slugify_for_dmn_id(text: str) -> str:
    """decision_id/rule_id 생성용 slug. 06-dmn.md 컨벤션(dmn_decision_<snake_case>)을 따른다."""
    import re
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", (text or "").strip()).strip("_")
    return slug.lower() if slug else "unnamed"


def merge_dmn_artifact_into_definition(definition: Dict[str, Any], artifact: Dict[str, Any]) -> Dict[str, Any]:
    """classify_and_extract_proposal이 만든 DMN_RULE artifact({"decision", "rules"})를
    proc_def.definition 사본에 병합한다. 원본은 변경하지 않는다.

    artifact의 decision/rule에는 id가 없으므로(분류 단계는 실제 definition을 보지
    않고 생성됨) decision 이름 기준으로 06-dmn.md 컨벤션(dmn_decision_<slug>,
    dmn_rule_<slug>_<순번>)에 맞는 id를 여기서 새로 만든다. 이미 같은 id의
    decision/rule이 있으면 중복 추가하지 않는다.
    """
    import copy

    merged = copy.deepcopy(definition) if isinstance(definition, dict) else {}

    decisions = merged.get("dmn_decisions")
    decisions = list(decisions) if isinstance(decisions, list) else []
    rules = merged.get("dmn_rules")
    rules = list(rules) if isinstance(rules, list) else []

    decision = artifact.get("decision") or {}
    decision_name = decision.get("name", "")
    decision_id = f"dmn_decision_{_slugify_for_dmn_id(decision_name)}"

    existing_decision_ids = {d.get("decision_id") for d in decisions if isinstance(d, dict)}
    if decision_id not in existing_decision_ids:
        decisions.append({
            "decision_id": decision_id,
            "name": decision_name,
            "description": decision.get("description", ""),
        })

    existing_rule_ids = {r.get("rule_id") for r in rules if isinstance(r, dict)}
    for idx, rule in enumerate(artifact.get("rules") or [], start=1):
        if not isinstance(rule, dict):
            continue
        rule_id = f"dmn_rule_{_slugify_for_dmn_id(decision_name)}_{idx}"
        if rule_id in existing_rule_ids:
            continue
        rules.append({
            "rule_id": rule_id,
            "decision_id": decision_id,
            "decision_name": decision_name,
            "when": rule.get("when", ""),
            "then": rule.get("then", ""),
            "condition": rule.get("condition", ""),
            "target": rule.get("target"),
        })
        existing_rule_ids.add(rule_id)

    merged["dmn_decisions"] = decisions
    merged["dmn_rules"] = rules
    return merged


def insert_draft_proc_def_version(
    tenant_id: str,
    proc_def_id: str,
    version: str,
    definition: Dict[str, Any],
    snapshot: str,
    message: str = "",
    parent_version: Optional[str] = None,
    source_todolist_id: Optional[str] = None,
    version_tag: str = "minor",
) -> Optional[Dict[str, Any]]:
    """DMN_RULE/PROCESS_DEFINITION target 승인 결과로 draft proc_def_version 행을
    만든다. 라이브 proc_def.definition은 건드리지 않는다 — is_draft=true인 별도
    행일 뿐이다.

    version_tag 기본값은 'minor'다(개별 DMN 규칙 추가는 major 변경이 아니라는
    기존 DMN_RULE 호출부 동작을 그대로 유지).
    arcv_id는 실측된 컨벤션(`{proc_def_id}_{version}`)을 따른다.
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid and version):
        return None

    try:
        supabase = get_db_client()
        row = {
            "arcv_id": f"{pdid}_{version}",
            "proc_def_id": pdid,
            "version": version,
            "version_tag": version_tag,
            "snapshot": snapshot,
            "definition": definition,
            "message": message,
            "tenant_id": tid,
            "parent_version": parent_version,
            "source_todolist_id": source_todolist_id,
            "is_draft": True,
        }
        resp = supabase.table("proc_def_version").insert(row).execute()
        data = resp.data or []
        return data[0] if data else None
    except Exception as e:
        handle_error("draft버전생성", e)
        return None


def insert_dmn_merge_request(
    tenant_id: str,
    proc_def_id: str,
    version: str,
    title: str,
    description: str,
    requester_ids: Optional[List[str]] = None,
    reviewer_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """DMN_RULE target 승인 결과로 resource_pull_requests에 병합 요청 행을 연다.

    SKILL PR과 같은 테이블/행 모양을 재사용하지만 실제 git 저장소는 없다 —
    dmn/bpmn 리소스는 이 테이블 안에서만 승인/이력 관리가 이루어진다(직접 확인됨).
    그래서 git_pr_number/git_pr_url/git_repo_url은 의도적으로 NULL로 남긴다.

    branch_name에는 이 병합 요청이 가리키는 draft proc_def_version.version 값을
    그대로 담는다 — 리뷰어가 어떤 draft 행에 대한 요청인지 branch_name만으로
    바로 알 수 있게 하기 위함이다(draft version 자체가 이미 유일한 접미사를
    가지므로 타임스탬프로 별도 유일성을 보장할 필요가 없다).

    requester_ids는 이 병합 요청을 촉발한 피드백 작성자들(배치의 collected_items
    user_id, 중복 제거)이고, reviewer_id는 target을 승인한 사람이다 — 승인자를
    requester로 기록하던 이전 동작은 fix-merge-request-requester에서 뒤집혔다.
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None

    try:
        supabase = get_db_client()
        row = {
            "tenant_id": tid,
            "resource_type": "dmn",
            "resource_id": pdid,
            "branch_name": version,
            "base_branch": "main",
            "title": title,
            "description": description,
            "status": "OPEN",
            "requester_id": requester_ids or [],
            "reviewer_id": reviewer_id,
        }
        resp = supabase.table("resource_pull_requests").insert(row).execute()
        data = resp.data or []
        return data[0] if data else None
    except Exception as e:
        handle_error("DMN병합요청생성", e)
        return None


# ============================================================================
# PROCESS_DEFINITION 승인 → proc_def_version draft + resource_pull_requests 병합 요청
# (add-process-definition-apply)
# ============================================================================

_PROCESS_DEFINITION_ARRAY_KINDS = {
    "activities": "activity",
    "sequences": "sequence",
    "gateways": "gateway",
}


def _slugify_for_element_id(kind: str, text: str) -> str:
    """activities/sequences/gateways용 요소 id 생성. _slugify_for_dmn_id와 같은 slug
    규칙을 쓰되 요소 종류로 네임스페이스를 둬(예: activity_<slug>) DMN id나 다른 종류의
    요소 id와 충돌하지 않게 한다.
    """
    return f"{kind}_{_slugify_for_dmn_id(text)}"


def _process_definition_element_and_basis(kind: str, raw_entry: Dict[str, Any]):
    """artifact 항목을 live 사본에 들어갈 요소 dict로 정규화하고, id 생성 기준
    텍스트(basis)를 함께 반환한다.

    activities/gateways는 artifact에 name이 있으므로 name을 기준으로 쓴다.
    sequences는 classify_and_extract_proposal의 artifact 스키마가 id/name 없이
    from/to만 준다(live 사본은 source/target을 쓴다) — from/to를 source/target으로
    옮겨 live 스키마에 맞추고, "from_to" 조합을 기준으로 id를 생성한다(같은 연결을
    가리키는 항목은 항상 같은 id로 귀결돼 자연히 dedup된다).
    """
    element = {k: v for k, v in raw_entry.items() if k != "change_type"}
    if kind == "sequence":
        source = element.pop("from", None) or element.get("source", "")
        target = element.pop("to", None) or element.get("target", "")
        element["source"] = source
        element["target"] = target
        basis = f"{source}_{target}"
    else:
        basis = element.get("name", "")
    return element, basis


def merge_process_definition_artifact_into_definition(
    definition: Dict[str, Any], artifact: Dict[str, Any]
):
    """classify_and_extract_proposal이 만든 PROCESS_DEFINITION artifact
    ({"summary", "activities", "sequences", "gateways"})를 proc_def.definition
    사본의 flattened 배열(activities/sequences/gateways)에 병합한다. 원본은
    변경하지 않는다.

    각 배열 항목은 change_type "ADD" 또는 "MODIFY"를 가진다:
    - ADD: id가 있고 live 복사본에 이미 있으면(혹은 이번 병합에서 이미 추가됐으면)
      중복 추가하지 않고 건너뛴다. id가 없으면 name(activities/gateways) 또는
      from/to 조합(sequences)으로부터 생성한다(DMN의 decision_id/rule_id
      컨벤션과 동일하게, 접미사로 유일성을 억지로 보장하지 않는다 — 생성된 id가
      이미 있으면 그것도 동일하게 dedup 대상이다).
    - MODIFY: id가 live 복사본의 기존 요소와 일치하면 그 요소를 갱신한다. 일치하지
      않으면(artifact는 실제 definition을 보지 않고 만들어지므로 흔한 경우다) 새
      요소로 강등해 추가한다 — 단 artifact가 지어낸 id는 재사용하지 않고 위 기준으로
      새로 생성한다(나중에 그 id를 가진 진짜 요소가 추가될 때 충돌하지 않도록).
      같은 미해결 artifact를 두 번 병합해도 생성된 id가 같아 두 번째는 dedup되어
      중복되지 않는다. artifact의 sequences 항목은 애초에 id를 갖지 않으므로
      (from/to만 있음) MODIFY로 와도 항상 강등된다 — 이는 스키마상 자연스러운
      결과다.

    반환값은 (병합된 definition 사본, MODIFY에서 ADD로 강등된 항목 수)다.
    """
    import copy

    merged = copy.deepcopy(definition) if isinstance(definition, dict) else {}
    demoted_count = 0

    for array_key, kind in _PROCESS_DEFINITION_ARRAY_KINDS.items():
        existing = merged.get(array_key)
        existing = list(existing) if isinstance(existing, list) else []
        existing_ids = {
            el.get("id") for el in existing if isinstance(el, dict) and el.get("id")
        }

        for raw_entry in artifact.get(array_key) or []:
            if not isinstance(raw_entry, dict):
                continue
            change_type = (raw_entry.get("change_type") or "").strip().upper()
            entry_id = (raw_entry.get("id") or "").strip()
            element, basis = _process_definition_element_and_basis(kind, raw_entry)

            if change_type == "MODIFY" and entry_id and entry_id in existing_ids:
                for idx, el in enumerate(existing):
                    if isinstance(el, dict) and el.get("id") == entry_id:
                        existing[idx] = {**el, **element, "id": entry_id}
                        break
                continue

            if change_type == "MODIFY":
                # 매칭되는 live 요소가 없음 → ADD로 강등. 지어낸 id는 재사용하지 않는다.
                demoted_count += 1
                new_id = _slugify_for_element_id(kind, basis)
                if new_id in existing_ids:
                    continue  # 이전에 이미 강등/추가된 것과 같은 요소 → dedup
                element["id"] = new_id
                existing.append(element)
                existing_ids.add(new_id)
                continue

            # ADD
            if entry_id:
                if entry_id in existing_ids:
                    continue  # 이미 있는 id → dedup, 건너뜀
                element["id"] = entry_id
                existing.append(element)
                existing_ids.add(entry_id)
            else:
                new_id = _slugify_for_element_id(kind, basis)
                if new_id in existing_ids:
                    continue
                element["id"] = new_id
                existing.append(element)
                existing_ids.add(new_id)

        merged[array_key] = existing

    return merged, demoted_count


def insert_bpmn_merge_request(
    tenant_id: str,
    proc_def_id: str,
    version: str,
    title: str,
    description: str,
    requester_ids: Optional[List[str]] = None,
    reviewer_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """PROCESS_DEFINITION target 승인 결과로 resource_pull_requests에 병합 요청 행을 연다.

    insert_dmn_merge_request와 동일한 테이블/행 모양을 재사용하지만
    resource_type='bpmn'이다 — 실제 git 저장소는 없다(DMN과 동일한 이유:
    dmn/bpmn 리소스는 이 테이블 안에서만 승인/이력 관리가 이루어진다).

    branch_name/requester_ids/reviewer_id 의미는 insert_dmn_merge_request와
    동일하다 — branch_name에는 draft proc_def_version.version 값을 그대로 담는다.
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None

    try:
        supabase = get_db_client()
        row = {
            "tenant_id": tid,
            "resource_type": "bpmn",
            "resource_id": pdid,
            "branch_name": version,
            "base_branch": "main",
            "title": title,
            "description": description,
            "status": "OPEN",
            "requester_id": requester_ids or [],
            "reviewer_id": reviewer_id,
        }
        resp = supabase.table("resource_pull_requests").insert(row).execute()
        data = resp.data or []
        return data[0] if data else None
    except Exception as e:
        handle_error("BPMN병합요청생성", e)
        return None


