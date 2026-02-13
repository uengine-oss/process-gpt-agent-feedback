"""
기존 지식 조회 모듈
각 저장소(mem0, DMN rules, skills)에서 기존 지식을 조회하는 기능
"""

import os
import asyncio
from typing import Dict, List, Optional, Any
from mem0 import Memory
from utils.logger import log, handle_error
from dotenv import load_dotenv
from core.database import get_db_client, _get_agent_by_id
from core.mcp_client import get_mcp_tools, get_mcp_tools_async, get_mcp_tool_by_name, get_mcp_tool_by_name_async
from core.skill_api_client import (
    check_skill_exists_with_info,
    get_skill_file_content,
    get_skill_files,
    list_uploaded_skills,
)

load_dotenv()

# ============================================================================
# 설정 및 초기화
# ============================================================================

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

if not all([DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME]):
    raise ValueError("❌ DB 연결 환경 변수가 설정되지 않았습니다. .env 파일을 확인해주세요.")

CONNECTION_STRING = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# 스킬 조회: 관련도 기반 선택·집중 (이 값 미만이면 컨텍스트에서 제외, None이면 제외 안 함)
MIN_SKILL_RELEVANCE_THRESHOLD = 0.5

# 스킬 검색 시 MCP find_helpful_skills에 넘길 작업 설명 길이 (목표+페르소나 일부로 시장 조사·보고서 등 관련 스킬 상위 노출)
SKILL_SEARCH_CONTEXT_CHARS = 220


# ============================================================================
# Memory (mem0) 조회
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


async def retrieve_existing_memories(agent_id: str, query: str, limit: int = 10) -> List[Dict]:
    """
    mem0에서 기존 메모리 조회 (semantic search 사용)
    
    Args:
        agent_id: 에이전트 ID
        query: 검색 쿼리 (피드백 내용과 유사한 기존 지식 검색)
        limit: 최대 결과 수
    
    Returns:
        기존 메모리 목록 [{"memory": "...", "score": 0.8, "id": "...", "metadata": {...}}, ...]
    """
    try:
        memory = _get_memory_instance()
        results = memory.search(query, agent_id=agent_id)
        hits = results.get("results", [])
        
        # 관련도가 높은 항목만 필터링 (threshold=0.5)
        THRESHOLD = 0.5
        filtered_hits = [h for h in hits if h.get("score", 0) >= THRESHOLD]
        
        # limit까지 반환
        return filtered_hits[:limit]
        
    except Exception as e:
        handle_error("기존메모리조회", e)
        return []


async def get_memories_by_agent(agent_id: str, limit: int = 100) -> List[Dict]:
    """
    PostgreSQL 함수를 사용하여 에이전트의 메모리 조회 (ID만 필요한 경우)
    
    Args:
        agent_id: 에이전트 ID
        limit: 최대 결과 수
    
    Returns:
        메모리 목록 (vecs.memories 테이블 구조)
    """
    try:
        supabase = get_db_client()
        resp = supabase.rpc('get_memories', {'agent': agent_id, 'lim': limit}).execute()
        return resp.data or []
    except Exception as e:
        handle_error("get_memories_함수조회", e)
        return []


async def delete_memories_by_agent(agent_id: str) -> None:
    """
    PostgreSQL 함수를 사용하여 에이전트의 모든 메모리 삭제
    
    Args:
        agent_id: 에이전트 ID
    """
    try:
        supabase = get_db_client()
        supabase.rpc('delete_memories_by_agent', {'agent': agent_id}).execute()
        log(f"🗑️ 에이전트 {agent_id}의 모든 메모리 삭제 완료")
    except Exception as e:
        handle_error("delete_memories_by_agent", e)
        raise


# ============================================================================
# DMN Rules 조회
# ============================================================================

async def retrieve_existing_dmn_rules(agent_id: str, search_text: str = "") -> List[Dict]:
    """
    proc_def 테이블에서 에이전트의 기존 DMN 규칙 조회
    
    Args:
        agent_id: 에이전트 ID (owner 필드로 필터링)
        search_text: 검색 키워드 (선택적, 조건/액션에서 검색)
    
    Returns:
        기존 DMN 규칙 목록 [{"id": "...", "name": "...", "bpmn": "...", ...}, ...]
    """
    try:
        supabase = get_db_client()
        
        # owner가 agent_id이고 type이 'dmn'인 항목 조회
        query = supabase.table('proc_def').select('*').eq('owner', agent_id).eq('type', 'dmn').eq('isdeleted', False)
        
        # 검색 키워드가 있으면 이름에서 검색
        # if search_text:
        #     query = query.ilike('name', f'%{search_text}%')
        
        resp = query.execute()
        return resp.data or []
        
    except Exception as e:
        handle_error("기존DMN규칙조회", e)
        return []


# ============================================================================
# Skills 조회 (MCP 서버를 통해)
# ============================================================================

def _parse_skill_markdown(text: str) -> List[Dict]:
    """
    MCP 서버가 반환한 마크다운 형식의 스킬 텍스트를 파싱하여 구조화된 스킬 리스트로 변환
    
    Args:
        text: 마크다운 형식의 스킬 정보 텍스트
        
    Returns:
        구조화된 스킬 딕셔너리 리스트
    """
    import re
    
    skills = []
    
    # "================================================================================" 구분자로 스킬 섹션 분리
    # 각 섹션은 "Skill N: [이름]" 형식으로 시작
    sections = re.split(r'={80,}', text)
    
    for section in sections:
        section = section.strip()
        if not section:
            continue
        
        # "Skill N: [이름]" 패턴 찾기
        skill_match = re.match(r'Skill\s+(\d+):\s*(.+)', section)
        if not skill_match:
            # 구분자 없이 시작하는 경우도 처리
            if section.startswith('Skill'):
                skill_match = re.match(r'Skill\s+(\d+):\s*(.+)', section)
            else:
                continue
        
        skill_num = skill_match.group(1)
        skill_name = skill_match.group(2).split('\n')[0].strip()  # 첫 줄만 추출
        
        skill = {
            "id": f"skill_{skill_num}",
            "name": skill_name,
            "skill_name": skill_name
        }
        
        # Relevance Score 추출
        relevance_match = re.search(r'Relevance Score:\s*([\d.]+)', section)
        if relevance_match:
            try:
                skill["relevance_score"] = float(relevance_match.group(1))
            except ValueError:
                pass
        
        # Source 추출
        source_match = re.search(r'Source:\s*(.+?)(?:\n|$)', section)
        if source_match:
            skill["source"] = source_match.group(1).strip()
            # source에서 ID 추출 시도 (경로나 URL에서)
            # 숫자만 있는 세그먼트(예: 순위/인덱스)는 스킬 ID로 쓰지 않음 → phantom SKILL:1 방지
            id_match = re.search(r'/([^/]+)/SKILL\.md', skill["source"])
            if id_match:
                path_segment = id_match.group(1)
                if not (path_segment.isdigit() or (path_segment.startswith("-") and path_segment[1:].isdigit())):
                    skill["id"] = path_segment
        
        # Scope 추출
        scope_match = re.search(r'Scope:\s*(.+?)(?:\n|$)', section)
        if scope_match:
            skill["scope"] = scope_match.group(1).strip()
        
        # Description 추출 (Description: 다음부터 다음 섹션까지)
        desc_match = re.search(r'Description:\s*(.+?)(?:\n\n-{80,}|$)', section, re.DOTALL)
        if desc_match:
            skill["description"] = desc_match.group(1).strip()
        
        # Full Content 추출 (Full Content: 다음부터 끝까지 또는 다음 스킬까지)
        content_match = re.search(r'Full Content:\s*\n\n(.+?)(?=\n={80,}|$)', section, re.DOTALL)
        if content_match:
            skill["content"] = content_match.group(1).strip()
        
        # name 기반으로 ID 생성 (아직 ID가 없거나 skill_N 형태인 경우)
        if "id" not in skill or skill["id"].startswith("skill_"):
            # source에서 마지막 경로 요소 추출 (숫자만 있는 세그먼트는 스킵)
            if "source" in skill:
                parts = re.split(r'[/\\]', skill["source"])
                for part in reversed(parts):
                    if part and part != "SKILL.md" and not part.endswith(".md"):
                        if not (part.isdigit() or (part.startswith("-") and part[1:].isdigit())):
                            skill["id"] = part
                            break
            
            # 여전히 ID가 없으면 name 기반 생성
            if "id" not in skill or skill["id"].startswith("skill_"):
                safe_id = re.sub(r'[^\w\s-]', '', skill_name)
                safe_id = re.sub(r'[-\s]+', '-', safe_id).lower()
                if safe_id:
                    skill["id"] = safe_id
        
        # 최소한의 정보가 있는 경우만 추가
        if "name" in skill and skill["name"]:
            skills.append(skill)
    
    return skills


def _normalize_skill_id(skill: Dict) -> None:
    """
    스킬 ID가 순위/인덱스용 숫자(또는 skill_N)일 경우 name/skill_name으로 치환.
    phantom SKILL:1 등 잘못된 레지스트리 등록 방지.
    """
    sid = skill.get("id")
    name = skill.get("name") or skill.get("skill_name") or ""
    if not name:
        return
    # int 또는 숫자 문자열이면 실제 스킬 이름으로 교체
    if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()):
        skill["id"] = name
        return
    # "-N" 형태
    if isinstance(sid, str) and len(sid) > 1 and sid.startswith("-") and sid[1:].isdigit():
        skill["id"] = name
        return
    # skill_1, skill_2 등 파싱용 임시 ID면 name으로 교체
    if isinstance(sid, str) and sid.startswith("skill_") and sid[5:].isdigit():
        skill["id"] = name
        return


def _extract_mcp_result_text(res: Any) -> str:
    """MCP 도구 결과에서 문자열 추출."""
    if res is None:
        return ""
    if isinstance(res, str):
        return res
    if isinstance(res, list):
        return "".join(
            str(x.get("text", "")) for x in res if isinstance(x, dict) and "text" in x
        )
    if isinstance(res, dict):
        c = res.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(
                str(x.get("text", "")) for x in c if isinstance(x, dict) and "text" in x
            )
        return res.get("text") or res.get("output") or ""
    return str(res)


async def _read_skill_document_mcp(skill_name: str, document_path: str = "SKILL.md") -> str:
    """MCP read_skill_document로 스킬 문서 내용 조회 (내장 스킬용)."""
    for tool_name in ("read_skill_document", "mcp_claude-skills_read_skill_document"):
        tool = await get_mcp_tool_by_name_async(tool_name)
        if tool is not None:
            try:
                out = await tool.ainvoke({
                    "skill_name": skill_name,
                    "document_path": document_path,
                })
                return (_extract_mcp_result_text(out) or "").strip()
            except Exception as e:
                log(f"   ⚠️ MCP read_skill_document 실패 ({skill_name}/{document_path}): {e}")
    return ""


async def retrieve_existing_skills(agent_id: str, search_text: str = "", top_k: int = 10, tenant_id: Optional[str] = None, agent_skills: Optional[str] = None) -> List[Dict]:
    """
    MCP 서버와 HTTP API를 통해 에이전트의 기존 스킬 조회
    
    벡터 유사도 검색(MCP 도구)과 정확한 스킬 존재 확인(HTTP API)을 결합하여 조회합니다.
    업로드된 스킬은 HTTP API로, 내장 스킬은 MCP read_skill_document 도구로 조회합니다.
    
    Args:
        agent_id: 에이전트 ID (현재는 사용되지 않지만 향후 확장 가능)
        search_text: 검색 키워드 또는 작업 설명 (task_description으로 사용)
                     특정 스킬 이름으로 보이는 경우 HTTP API를 우선 사용
        top_k: 최대 반환할 스킬 개수 (기본값: 10)
        tenant_id: 테넌트 ID (MCP 서버에 전달)
        agent_skills: 데이터베이스에 저장된 에이전트의 기존 스킬 목록 (선택적)
    
    Returns:
        기존 스킬 목록 (HTTP API와 MCP 서버 응답 형식을 통합)
    """
    try:
        log(
            f"🔍 스킬 조회 시작 (MCP + HTTP API): "
            f"agent_id={agent_id}, search_text={search_text[:50] if search_text else 'None'}..., tenant_id={tenant_id or 'None'}, agent_skills={agent_skills or 'None'}..."
        )

        results: List[Dict] = []
        skill_names_found = set()  # 중복 제거용
        
        # 업로드된 스킬 목록 조회 (HTTP API로 조회 가능한 스킬만 확인)
        uploaded_skills_set = set()
        try:
            uploaded_skills = list_uploaded_skills()
            uploaded_skills_set = {skill.get("name", "") for skill in uploaded_skills if skill.get("name")}
            log(f"   📋 업로드된 스킬 목록: {len(uploaded_skills_set)}개")
        except Exception as e:
            log(f"   ⚠️ 업로드된 스킬 목록 조회 실패: {e}")
        
        # agent_skills에 있는 스킬들도 HTTP API로 확인하여 업로드된 스킬인지 검증
        # (list_uploaded_skills가 실패하거나 빈 결과를 반환해도 agent_skills에 있는 스킬은 확인 필요)
        if agent_skills:
            allowed_skill_names = [s.strip() for s in agent_skills.split(",") if s.strip()]
            for skill_name in allowed_skill_names:
                if skill_name not in uploaded_skills_set:
                    # HTTP API로 실제 존재 여부 확인
                    try:
                        skill_info = check_skill_exists_with_info(skill_name)
                        if skill_info and skill_info.get("exists"):
                            uploaded_skills_set.add(skill_name)
                            log(f"   ✅ agent_skills에서 업로드된 스킬 확인: {skill_name}")
                    except Exception as e:
                        log(f"   ⚠️ agent_skills 스킬 확인 실패 ({skill_name}): {e}")
        
        if uploaded_skills_set:
            log(f"   📋 최종 업로드된 스킬 목록: {len(uploaded_skills_set)}개 ({', '.join(list(uploaded_skills_set)[:5])}{'...' if len(uploaded_skills_set) > 5 else ''})")
        
        # 1. 특정 스킬 이름으로 검색하는 경우
        # (search_text가 짧고 특정 스킬 이름처럼 보이는 경우)
        if search_text and len(search_text.strip()) < 100:
            skill_name_candidate = search_text.strip()
            log(f"   🔍 특정 스킬 이름으로 검색: '{skill_name_candidate}'")
            
            # 업로드된 스킬인 경우 HTTP API 사용
            if skill_name_candidate in uploaded_skills_set:
                try:
                    skill_info = check_skill_exists_with_info(skill_name_candidate)
                    if skill_info and skill_info.get("exists"):
                        skill_name = skill_info.get("name", skill_name_candidate)
                        try:
                            skill_file_info = get_skill_file_content(skill_name, "SKILL.md")
                            skill_content = skill_file_info.get("content", "")
                            
                            skill_dict = {
                                "id": skill_name,
                                "name": skill_name,
                                "skill_name": skill_name,
                                "description": skill_info.get("description", ""),
                                "source": skill_info.get("source", ""),
                                "document_count": skill_info.get("document_count", 0),
                                "content": skill_content,
                                "verified": True,
                            }
                            
                            results.append(skill_dict)
                            skill_names_found.add(skill_name)
                            log(f"   ✅ HTTP API를 통해 업로드된 스킬 확인: {skill_name}")
                        except Exception as e:
                            log(f"   ⚠️ 업로드된 스킬 파일 조회 실패 ({skill_name}): {e}")
                except Exception as e:
                    log(f"   ⚠️ HTTP API 스킬 확인 실패: {e}")
            else:
                # 업로드된 스킬이 아니면 MCP(내장 스킬)로 조회
                content = await _read_skill_document_mcp(skill_name_candidate, "SKILL.md")
                if content:
                    skill_dict = {
                        "id": skill_name_candidate,
                        "name": skill_name_candidate,
                        "skill_name": skill_name_candidate,
                        "description": "",
                        "source": "",
                        "document_count": 0,
                        "content": content,
                        "verified": False,
                        "is_builtin": True,
                    }
                    results.append(skill_dict)
                    skill_names_found.add(skill_name_candidate)
                    log(f"   ✅ 내장 스킬 조회 (MCP): {skill_name_candidate}")

        # 2. MCP 도구를 통한 벡터 유사도 검색 (작업 설명 기반 검색)
        try:
            tools = await get_mcp_tools_async()
            find_skills_tool = None
            for tool in tools:
                if getattr(tool, "name", None) == "find_helpful_skills":
                    find_skills_tool = tool
                    break
            
            if find_skills_tool is not None:
                # 작업 설명이 없으면 기본값 사용
                task_description = search_text if search_text else "일반적인 작업 수행"

                # find_helpful_skills 도구 호출 파라미터 구성
                invoke_params = {
                    "task_description": task_description,
                    "top_k": top_k,
                    "list_documents": True,  # 문서 목록도 함께 조회
                }
                
                # tenant_id가 제공된 경우에만 추가
                if tenant_id:
                    invoke_params["tenant_id"] = tenant_id

                if agent_skills:
                    # 공백 제거하여 스킬 이름 배열 생성
                    allowed_skill_names = [s.strip() for s in agent_skills.split(",") if s.strip()]
                    if allowed_skill_names:
                        invoke_params["allowed_skill_names"] = allowed_skill_names

                # find_helpful_skills 도구 호출 (비동기 방식)
                log(
                    f"   🔍 MCP 도구를 통한 벡터 검색: "
                    f"task_description='{task_description[:100]}...', top_k={top_k}, tenant_id={tenant_id or 'None'}"
                )
                # 타임아웃 추가 (30초)
                try:
                    mcp_result = await asyncio.wait_for(
                        find_skills_tool.ainvoke(invoke_params),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    log(f"   ⚠️ MCP find_helpful_skills 타임아웃, 벡터 검색 건너뜀")
                    mcp_result = None

                # MCP 결과 파싱 (타임아웃 시 빈 리스트)
                mcp_skills = _parse_mcp_skill_result(mcp_result) if mcp_result is not None else []
                
                for mcp_skill in mcp_skills:
                    skill_name = mcp_skill.get("name") or mcp_skill.get("skill_name", "")
                    if not skill_name or skill_name in skill_names_found:
                        continue
                    
                    is_uploaded_skill = skill_name in uploaded_skills_set
                    if not is_uploaded_skill and agent_skills:
                        allowed_skill_names = [s.strip() for s in agent_skills.split(",") if s.strip()]
                        if skill_name in allowed_skill_names:
                            try:
                                skill_info = check_skill_exists_with_info(skill_name)
                                if skill_info and skill_info.get("exists"):
                                    is_uploaded_skill = True
                                    uploaded_skills_set.add(skill_name)
                                    log(f"   ✅ agent_skills에서 업로드된 스킬 확인 (벡터 검색 결과): {skill_name}")
                            except Exception as e:
                                log(f"   ⚠️ agent_skills 스킬 확인 실패 ({skill_name}): {e}")
                    
                    if is_uploaded_skill:
                        try:
                            skill_info = check_skill_exists_with_info(skill_name)
                            if skill_info and skill_info.get("exists"):
                                try:
                                    skill_file_info = get_skill_file_content(skill_name, "SKILL.md")
                                    skill_content = skill_file_info.get("content", "")
                                    
                                    combined_skill = {
                                        **mcp_skill,
                                        "id": skill_name,
                                        "name": skill_name,
                                        "skill_name": skill_name,
                                        "description": skill_info.get("description", mcp_skill.get("description", "")),
                                        "source": skill_info.get("source", mcp_skill.get("source", "")),
                                        "document_count": skill_info.get("document_count", 0),
                                        "content": skill_content if skill_content else mcp_skill.get("content", ""),
                                        "verified": True,
                                        "is_builtin": False,
                                    }
                                    
                                    results.append(combined_skill)
                                    skill_names_found.add(skill_name)
                                    log(f"   ✅ 업로드된 스킬 (HTTP API): {skill_name}")
                                except Exception as e:
                                    log(f"   ⚠️ 업로드된 스킬 파일 조회 실패 ({skill_name}): {e}")
                                    combined_skill = {
                                        **mcp_skill,
                                        "id": skill_name,
                                        "name": skill_name,
                                        "skill_name": skill_name,
                                        "description": skill_info.get("description", mcp_skill.get("description", "")),
                                        "source": skill_info.get("source", mcp_skill.get("source", "")),
                                        "document_count": skill_info.get("document_count", 0),
                                        "verified": True,
                                        "is_builtin": False,
                                    }
                                    results.append(combined_skill)
                                    skill_names_found.add(skill_name)
                        except Exception as e:
                            log(f"   ⚠️ 업로드된 스킬 HTTP API 확인 실패 ({skill_name}): {e}")
                            mcp_skill["verified"] = False
                            mcp_skill["is_builtin"] = False
                            mcp_skill["id"] = skill_name
                            mcp_skill["name"] = skill_name
                            mcp_skill["skill_name"] = skill_name
                            results.append(mcp_skill)
                            skill_names_found.add(skill_name)
                    else:
                        # 내장 스킬: MCP read_skill_document로 상세 조회
                        content = await _read_skill_document_mcp(skill_name, "SKILL.md")
                        combined_skill = {
                            **mcp_skill,
                            "id": skill_name,
                            "name": skill_name,
                            "skill_name": skill_name,
                            "description": mcp_skill.get("description", ""),
                            "source": mcp_skill.get("source", ""),
                            "document_count": mcp_skill.get("document_count", 0),
                            "content": content or mcp_skill.get("content", ""),
                            "verified": bool(content),
                            "is_builtin": True,
                        }
                        results.append(combined_skill)
                        skill_names_found.add(skill_name)
                        log(f"   ✅ 내장 스킬 (MCP): {skill_name}")
            else:
                tool_names = [t.name for t in tools if hasattr(t, "name")]
                log(f"   ⚠️ find_helpful_skills 도구를 찾을 수 없습니다. 사용 가능한 도구: {tool_names}")
        except Exception as e:
            log(f"   ⚠️ MCP 도구를 통한 스킬 검색 실패: {e}")

        # 3. agent_skills에 명시된 스킬들도 확인
        if agent_skills:
            allowed_skill_names = [s.strip() for s in agent_skills.split(",") if s.strip()]
            for skill_name in allowed_skill_names:
                if skill_name in skill_names_found:
                    continue
                
                if skill_name in uploaded_skills_set:
                    try:
                        skill_info = check_skill_exists_with_info(skill_name)
                        if skill_info and skill_info.get("exists"):
                            try:
                                skill_file_info = get_skill_file_content(skill_name, "SKILL.md")
                                skill_content = skill_file_info.get("content", "")
                                
                                skill_dict = {
                                    "id": skill_name,
                                    "name": skill_name,
                                    "skill_name": skill_name,
                                    "description": skill_info.get("description", ""),
                                    "source": skill_info.get("source", ""),
                                    "document_count": skill_info.get("document_count", 0),
                                    "content": skill_content,
                                    "verified": True,
                                    "is_builtin": False,
                                }
                                
                                results.append(skill_dict)
                                skill_names_found.add(skill_name)
                                log(f"   ✅ agent_skills에서 업로드된 스킬 확인: {skill_name}")
                            except Exception as e:
                                log(f"   ⚠️ 업로드된 스킬 파일 조회 실패 ({skill_name}): {e}")
                    except Exception as e:
                        log(f"   ⚠️ agent_skills 업로드된 스킬 확인 실패 ({skill_name}): {e}")
                else:
                    # 내장 스킬: MCP로 조회
                    content = await _read_skill_document_mcp(skill_name, "SKILL.md")
                    skill_dict = {
                        "id": skill_name,
                        "name": skill_name,
                        "skill_name": skill_name,
                        "description": "",
                        "source": "",
                        "document_count": 0,
                        "content": content,
                        "verified": bool(content),
                        "is_builtin": True,
                    }
                    results.append(skill_dict)
                    skill_names_found.add(skill_name)
                    log(f"   ✅ agent_skills에서 내장 스킬 조회 (MCP): {skill_name}")

        # 관련도 임계값 미만 스킬 제외 (relevance_score 없으면 유지: 정확 매칭·agent_skills 등)
        results = [
            r for r in results
            if r.get("relevance_score") is None
            or (r.get("relevance_score") or 0) >= MIN_SKILL_RELEVANCE_THRESHOLD
        ]
        # 스킬 ID가 숫자/인덱스 형태면 name 기반으로 정규화 (phantom SKILL:1 방지)
        for r in results:
            _normalize_skill_id(r)
        # 관련도 1순위, verified 2순위로 정렬 후 top_k만 반환
        results.sort(key=lambda x: ((x.get("relevance_score") or 0), x.get("verified", False)), reverse=True)
        
        log(f"✅ 스킬 조회 완료: 총 {len(results)}개 스킬 발견 (HTTP API 검증: {sum(1 for s in results if s.get('verified', False))}개)")
        return results[:top_k]  # top_k만큼만 반환

    except Exception as e:
        handle_error("기존스킬조회", e)
        log(f"⚠️ 스킬 조회 실패: {e}")
        return []


def _parse_mcp_skill_result(result: Any) -> List[Dict]:
    """
    MCP 도구 결과를 파싱하여 스킬 리스트로 변환
    
    Args:
        result: MCP 도구 반환 결과 (다양한 형식 가능)
    
    Returns:
        구조화된 스킬 딕셔너리 리스트
    """
    import re
    import json
    
    parsed_skills = []
    
    # LangChain ToolMessage 또는 content blocks 형식 처리
    if isinstance(result, list):
        # 리스트인 경우 - content blocks 형식일 수 있음
        full_text = ""
        
        for item in result:
            if isinstance(item, dict):
                # content block 형식
                if item.get("type") == "text" and "text" in item:
                    full_text += item["text"] + "\n\n"
                # 이미 구조화된 스킬 객체인 경우
                elif "name" in item or "skill_name" in item or "id" in item:
                    parsed_skills.append(item)
        
        # 텍스트가 모인 경우 마크다운 파싱
        if full_text and not parsed_skills:
            parsed_skills = _parse_skill_markdown(full_text)
        
        if parsed_skills:
            for s in parsed_skills:
                _normalize_skill_id(s)
            return parsed_skills
        
        # 구조화되지 않은 리스트인 경우 그대로 반환
        return result if isinstance(result, list) else []

    if isinstance(result, dict):
        # 딕셔너리인 경우 skills/results 필드를 우선적으로 사용
        skills = result.get("skills", result.get("results", []))
        if isinstance(skills, list):
            for s in skills:
                _normalize_skill_id(s)
            return skills
        else:
            # 단일 스킬인 경우 리스트로 변환
            if skills:
                _normalize_skill_id(skills)
            return [skills] if skills else []

    if isinstance(result, str):
        # 문자열인 경우 파싱 시도
        # JSON일 수 있음
        try:
            parsed = json.loads(result)
            if isinstance(parsed, list):
                for s in parsed:
                    _normalize_skill_id(s)
                return parsed
            if isinstance(parsed, dict):
                skills = parsed.get("skills", parsed.get("results", []))
                if isinstance(skills, list):
                    for s in skills:
                        _normalize_skill_id(s)
                return skills
        except Exception:
            pass
        
        # 마크다운 텍스트인 경우 파싱
        parsed_skills = _parse_skill_markdown(result)
        if parsed_skills:
            for s in parsed_skills:
                _normalize_skill_id(s)
            return parsed_skills

    return []


# ============================================================================
# 통합 조회
# ============================================================================

async def retrieve_all_existing_knowledge(agent_id: str, feedback_content: str) -> Dict:
    """
    모든 저장소에서 기존 지식을 조회하여 반환
    
    Args:
        agent_id: 에이전트 ID
        feedback_content: 피드백 내용 (검색 쿼리로 사용)
    
    Returns:
        {
            "memories": [...],
            "dmn_rules": [...],
            "skills": [...]
        }
    """
    try:
        log(f"🔍 기존 지식 조회 시작: agent_id={agent_id}")
        
        # 에이전트 정보 조회하여 tenant_id 가져오기
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None
        agent_skills = agent_info.get("skills") if agent_info else None
        
        # 각 저장소에서 조회 (병렬 처리)
        memories = await retrieve_existing_memories(agent_id, feedback_content, limit=10)
        dmn_rules = await retrieve_existing_dmn_rules(agent_id, feedback_content[:100])  # 검색용으로 앞부분만 사용
        skills = await retrieve_existing_skills(agent_id, feedback_content[:SKILL_SEARCH_CONTEXT_CHARS], top_k=3, tenant_id=tenant_id, agent_skills=agent_skills)
        
        log(f"📊 기존 지식 조회 완료: memories={len(memories)}, dmn_rules={len(dmn_rules)}, skills={len(skills)}")
        
        return {
            "memories": memories,
            "dmn_rules": dmn_rules,
            "skills": skills
        }
        
    except Exception as e:
        handle_error("통합기존지식조회", e)
        return {
            "memories": [],
            "dmn_rules": [],
            "skills": []
        }

