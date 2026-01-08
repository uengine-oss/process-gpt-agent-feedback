"""
ReAct 에이전트용 도구 정의
기존 함수들을 LangChain Tool로 래핑
"""

import json
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from utils.logger import log, handle_error

# 기존 모듈 import
from core.knowledge_retriever import (
    retrieve_existing_memories,
    retrieve_existing_dmn_rules,
    retrieve_existing_skills,
    retrieve_all_existing_knowledge
)
from core.learning_committers import (
    commit_to_memory,
    commit_to_dmn_rule,
    commit_to_skill
)
from core.conflict_analyzer import analyze_knowledge_conflict
from core.semantic_matcher import get_semantic_matcher


# ============================================================================
# 도구 입력 스키마 정의
# ============================================================================

class SearchMemoryInput(BaseModel):
    """메모리 검색 도구 입력"""
    query: str = Field(..., description="검색 쿼리 (피드백 내용 또는 키워드)")
    limit: int = Field(default=10, description="최대 결과 수")


class SearchDmnRulesInput(BaseModel):
    """DMN 규칙 검색 도구 입력"""
    search_text: str = Field(default="", description="검색 키워드")


class SearchSkillsInput(BaseModel):
    """Skills 검색 도구 입력"""
    search_text: str = Field(default="", description="검색 키워드 또는 작업 설명")
    top_k: int = Field(default=10, description="최대 결과 수")


class AnalyzeConflictInput(BaseModel):
    """충돌 분석 도구 입력"""
    new_knowledge_json: str = Field(..., description="새로운 지식을 JSON 문자열로 전달 (예: '{\"content\": \"...\"}' 또는 '{\"dmn\": {\"name\": \"...\", \"condition\": \"...\", \"action\": \"...\"}}' 또는 '{\"skill\": {\"name\": \"...\", \"steps\": [...]}}')")
    existing_knowledge_json: str = Field(..., description="기존 지식을 JSON 문자열로 전달 (예: '{\"memories\": [...], \"dmn_rules\": [...], \"skills\": [...]}')")
    target_type: str = Field(..., description="저장 대상 타입 (MEMORY | DMN_RULE | SKILL)")


class CommitMemoryInput(BaseModel):
    """메모리 저장 도구 입력"""
    content: str = Field(..., description="저장할 메모리 내용")
    operation: str = Field(default="CREATE", description="작업 타입 (CREATE | UPDATE | DELETE)")
    memory_id: Optional[str] = Field(default=None, description="UPDATE/DELETE 시 기존 메모리 ID")


class CommitDmnRuleInput(BaseModel):
    """DMN 규칙 저장 도구 입력"""
    dmn_artifact_json: str = Field(..., description="DMN 규칙 정보를 JSON 문자열로 전달. 단일 규칙: '{\"name\": \"규칙 이름\", \"condition\": \"조건\", \"action\": \"결과\"}'. 여러 규칙: '{\"name\": \"규칙 이름\", \"rules\": [{\"condition\": \"조건1\", \"action\": \"결과1\"}, {\"condition\": \"조건2\", \"action\": \"결과2\"}]}'. 여러 규칙이 있으면 자동으로 병합됩니다.")
    operation: str = Field(default="CREATE", description="⚠️ 작업 타입 (CREATE | UPDATE | DELETE). 유사한 기존 규칙이 있으면 반드시 UPDATE를 사용하고 rule_id를 함께 전달하세요!")
    rule_id: Optional[str] = Field(default=None, description="⚠️ UPDATE/DELETE 시 필수! 기존 규칙 ID (search_similar_knowledge 또는 search_dmn_rules 결과에서 얻은 ID)")
    feedback_content: str = Field(default="", description="원본 피드백 내용 (선택적)")


class CommitSkillInput(BaseModel):
    """Skill 저장 도구 입력"""
    skill_artifact_json: str = Field(..., description="Skill 정보를 JSON 문자열로 전달. 필수 필드: description (frontmatter용), overview (본문 개요), steps (단계별 절차). 선택 필드: usage (사용법), additional_files (scripts/ 폴더에 Python 파일 포함 시). 예: '{\"name\": \"스킬 이름\", \"description\": \"간단한 설명\", \"overview\": \"상세 개요\", \"steps\": [\"1단계\", \"2단계\", ...], \"usage\": \"사용법\", \"additional_files\": {\"scripts/helper.py\": \"코드\"}}'")
    operation: str = Field(default="CREATE", description="작업 타입 (CREATE | UPDATE | DELETE)")
    skill_id: Optional[str] = Field(default=None, description="UPDATE/DELETE 시 기존 스킬 ID")


# ============================================================================
# 새로운 통합 도구 스키마 (Phase 2: 의미적 유사도 기반)
# ============================================================================

class SearchSimilarKnowledgeInput(BaseModel):
    """통합 유사 지식 검색 도구 입력 (단순화)"""
    content: str = Field(..., description="검색할 지식 내용 (피드백 내용 또는 저장하려는 지식)")
    knowledge_type: str = Field(
        default="ALL",
        description="검색 대상 타입 (MEMORY | DMN_RULE | SKILL | ALL)"
    )
    threshold: float = Field(
        default=0.7,
        description="유사도 임계값 (0.0-1.0). 이 값 이상의 유사도를 가진 지식만 반환"
    )


class CheckDuplicateInput(BaseModel):
    """중복 확인 도구 입력 (단순화)"""
    content: str = Field(..., description="중복 여부를 확인할 새로운 지식 내용")
    knowledge_type: str = Field(..., description="지식 타입 (MEMORY | DMN_RULE | SKILL)")
    candidate_id: Optional[str] = Field(
        default=None,
        description="특정 기존 지식과 비교할 경우 해당 ID. 없으면 모든 기존 지식과 비교"
    )


class DetermineOperationInput(BaseModel):
    """작업 결정 도구 입력 (단순화)"""
    content: str = Field(..., description="저장하려는 새로운 지식 내용")
    knowledge_type: str = Field(..., description="지식 타입 (MEMORY | DMN_RULE | SKILL)")


class GetKnowledgeDetailInput(BaseModel):
    """기존 지식 상세 조회 도구 입력"""
    knowledge_type: str = Field(..., description="지식 타입 (MEMORY | DMN_RULE | SKILL)")
    knowledge_id: str = Field(default="", description="조회할 지식 ID (필수)")


# ============================================================================
# 도구 함수 정의
# ============================================================================

async def _search_memory_tool(agent_id: str, query: str, limit: int = 10) -> str:
    """
    mem0에서 관련 메모리를 검색합니다.
    
    Args:
        agent_id: 에이전트 ID
        query: 검색 쿼리
        limit: 최대 결과 수
    
    Returns:
        검색 결과 (포맷된 텍스트)
    """
    try:
        memories = await retrieve_existing_memories(agent_id, query, limit)
        
        if not memories:
            return "관련 메모리가 없습니다."
        
        result_lines = [f"총 {len(memories)}개의 관련 메모리를 찾았습니다:\n"]
        for idx, mem in enumerate(memories, start=1):
            memory_text = mem.get("memory", "")
            score = mem.get("score", 0)
            mem_id = mem.get("id", f"memory_{idx}")
            result_lines.append(f"[{idx}] ID: {mem_id}, 관련도: {score:.2f}")
            result_lines.append(f"    내용: {memory_text[:300]}...")
            result_lines.append("")
        
        return "\n".join(result_lines)
    except Exception as e:
        handle_error("search_memory_tool", e)
        return f"메모리 검색 실패: {str(e)}"


async def _search_dmn_rules_tool(agent_id: str, search_text: str = "") -> str:
    """
    DMN 규칙을 검색합니다.
    
    Args:
        agent_id: 에이전트 ID
        search_text: 검색 키워드
    
    Returns:
        검색 결과 (포맷된 텍스트)
    """
    try:
        rules = await retrieve_existing_dmn_rules(agent_id, search_text)
        
        if not rules:
            return "관련 DMN 규칙이 없습니다."
        
        result_lines = [f"총 {len(rules)}개의 DMN 규칙을 찾았습니다:\n"]
        for idx, rule in enumerate(rules, start=1):
            rule_id = rule.get("id", "")
            rule_name = rule.get("name", "")
            bpmn = rule.get("bpmn", "")
            result_lines.append(f"[{idx}] ID: {rule_id}, 이름: {rule_name}")
            result_lines.append(f"    XML 내용: {bpmn[:200]}...")
            result_lines.append("")
        
        return "\n".join(result_lines)
    except Exception as e:
        handle_error("search_dmn_rules_tool", e)
        return f"DMN 규칙 검색 실패: {str(e)}"


async def _search_skills_tool(agent_id: str, search_text: str = "", top_k: int = 10) -> str:
    """
    Skills를 검색합니다.
    
    Args:
        agent_id: 에이전트 ID
        search_text: 검색 키워드 또는 작업 설명
        top_k: 최대 결과 수
    
    Returns:
        검색 결과 (포맷된 텍스트)
    """
    try:
        # 에이전트 정보 조회하여 tenant_id 가져오기
        from core.database import _get_agent_by_id
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None
        agent_skills = agent_info.get("skills") if agent_info else None
        
        skills = await retrieve_existing_skills(agent_id, search_text, top_k, tenant_id=tenant_id, agent_skills=agent_skills)
        
        if not skills:
            return "관련 Skills가 없습니다."
        
        result_lines = [f"총 {len(skills)}개의 Skills를 찾았습니다:\n"]
        for idx, skill in enumerate(skills, start=1):
            skill_id = skill.get("id", skill.get("name", f"skill_{idx}"))
            skill_name = skill.get("name", skill.get("skill_name", "Unknown"))
            result_lines.append(f"[{idx}] ID: {skill_id}, 이름: {skill_name}")
            if "description" in skill:
                result_lines.append(f"    설명: {skill['description'][:200]}...")
            result_lines.append("")
        
        return "\n".join(result_lines)
    except Exception as e:
        handle_error("search_skills_tool", e)
        return f"Skills 검색 실패: {str(e)}"


async def _analyze_conflict_tool(
    new_knowledge: Dict,
    existing_knowledge: Dict,
    target_type: str
) -> str:
    """
    새로운 지식과 기존 지식 간의 충돌을 분석합니다.
    
    Args:
        new_knowledge: 새로운 지식 (content, dmn, skill 중 하나)
        existing_knowledge: 기존 지식 (memories, dmn_rules, skills 포함)
        target_type: 저장 대상 타입 (MEMORY | DMN_RULE | SKILL)
    
    Returns:
        충돌 분석 결과 (JSON 문자열)
    """
    try:
        result = await analyze_knowledge_conflict(new_knowledge, existing_knowledge, target_type)
        
        # 결과를 읽기 쉬운 형식으로 포맷팅
        operation = result.get("operation", "CREATE")
        conflict_level = result.get("conflict_level", "NONE")
        conflict_reason = result.get("conflict_reason", "")
        matched_item = result.get("matched_item")
        action_description = result.get("action_description", "")
        
        result_text = f"""충돌 분석 결과:
- 작업: {operation}
- 충돌 수준: {conflict_level}
- 이유: {conflict_reason}
- 작업 설명: {action_description}"""
        
        if matched_item:
            matched_id = matched_item.get("id", "Unknown")
            matched_content = matched_item.get("content", "")
            result_text += f"\n- 매칭된 항목 ID: {matched_id}"
            if matched_content:
                result_text += f"\n- 매칭된 항목 내용: {matched_content[:200]}..."
        
        return result_text
    except Exception as e:
        handle_error("analyze_conflict_tool", e)
        return f"충돌 분석 실패: {str(e)}"


async def _commit_memory_tool(
    agent_id: str,
    content: str,
    operation: str = "CREATE",
    memory_id: Optional[str] = None
) -> str:
    """
    mem0에 메모리를 저장/수정/삭제합니다.
    
    Args:
        agent_id: 에이전트 ID
        content: 저장할 내용
        operation: CREATE | UPDATE | DELETE
        memory_id: UPDATE/DELETE 시 기존 메모리 ID
    
    Returns:
        작업 결과 메시지
    """
    try:
        await commit_to_memory(
            agent_id=agent_id,
            content=content,
            source_type="guideline",
            operation=operation,
            memory_id=memory_id
        )
        
        if operation == "CREATE":
            return f"✅ 메모리가 성공적으로 저장되었습니다. (에이전트: {agent_id})"
        elif operation == "UPDATE":
            return f"✅ 메모리가 성공적으로 수정되었습니다. (ID: {memory_id}, 에이전트: {agent_id})"
        elif operation == "DELETE":
            return f"✅ 메모리가 성공적으로 삭제되었습니다. (ID: {memory_id}, 에이전트: {agent_id})"
        else:
            return f"⚠️ 알 수 없는 작업: {operation}"
    except Exception as e:
        handle_error("commit_memory_tool", e)
        return f"❌ 메모리 저장 실패: {str(e)}"


async def _commit_dmn_rule_tool(
    agent_id: str,
    dmn_artifact: Dict,
    operation: str = "CREATE",
    rule_id: Optional[str] = None,
    feedback_content: str = ""
) -> str:
    """
    DMN 규칙을 저장/수정/삭제합니다.
    
    Args:
        agent_id: 에이전트 ID
        dmn_artifact: DMN 규칙 정보 (name, condition, action 포함)
        operation: CREATE | UPDATE | DELETE
        rule_id: UPDATE/DELETE 시 기존 규칙 ID
        feedback_content: 원본 피드백 내용 (선택적)
    
    Returns:
        작업 결과 메시지
    """
    try:
        await commit_to_dmn_rule(
            agent_id=agent_id,
            dmn_artifact=dmn_artifact,
            feedback_content=feedback_content,
            operation=operation,
            rule_id=rule_id
        )
        
        rule_name = dmn_artifact.get("name", "Unknown")
        if operation == "CREATE":
            return f"✅ DMN 규칙이 성공적으로 저장되었습니다. (이름: {rule_name}, 에이전트: {agent_id})"
        elif operation == "UPDATE":
            return f"✅ DMN 규칙이 성공적으로 수정되었습니다. (ID: {rule_id}, 이름: {rule_name}, 에이전트: {agent_id})"
        elif operation == "DELETE":
            return f"✅ DMN 규칙이 성공적으로 삭제되었습니다. (ID: {rule_id}, 에이전트: {agent_id})"
        else:
            return f"⚠️ 알 수 없는 작업: {operation}"
    except Exception as e:
        handle_error("commit_dmn_rule_tool", e)
        return f"❌ DMN 규칙 저장 실패: {str(e)}"


async def _commit_skill_tool(
    agent_id: str,
    skill_artifact: Dict,
    operation: str = "CREATE",
    skill_id: Optional[str] = None
) -> str:
    """
    Skill을 저장/수정/삭제합니다.
    
    CREATE 작업 시 기존 스킬을 확인하여 중복되면 UPDATE로 전환하고,
    중복된 스킬이 있으면 삭제합니다.
    
    Args:
        agent_id: 에이전트 ID
        skill_artifact: Skill 정보 (name, steps 포함)
        operation: CREATE | UPDATE | DELETE
        skill_id: UPDATE/DELETE 시 기존 스킬 ID
    
    Returns:
        작업 결과 메시지
    """
    try:
        skill_name = skill_artifact.get("name", skill_id or "피드백 기반 스킬")
        
        # CREATE 작업 시 기존 스킬 확인 및 중복 처리
        if operation == "CREATE":
            # 기존 스킬 조회
            from core.database import _get_agent_by_id, update_agent_and_tenant_skills
            from core.skill_api_client import delete_skill
            
            agent_info = _get_agent_by_id(agent_id)
            tenant_id = agent_info.get("tenant_id") if agent_info else None
            agent_skills = agent_info.get("skills") if agent_info else None
            
            # 스킬 이름으로 기존 스킬 검색
            existing_skills_all = await retrieve_existing_skills(
                agent_id, 
                skill_name, 
                top_k=20, 
                tenant_id=tenant_id, 
                agent_skills=agent_skills
            )
            
            # 업로드된 스킬만 필터링 (기본 내장 스킬 제외, HTTP API로 검증된 스킬만 사용)
            existing_skills = [
                skill for skill in existing_skills_all 
                if not skill.get("is_builtin", False) and skill.get("verified", False)
            ]
            
            log(f"🔍 업로드된 스킬 검색 결과: {len(existing_skills)}개 (전체: {len(existing_skills_all)}개, 기본 내장 스킬 및 미검증 스킬 제외)")
            
            # 정확히 일치하는 스킬 이름 찾기
            exact_match = None
            duplicate_skills = []
            
            for existing_skill in existing_skills:
                existing_name = existing_skill.get("name", existing_skill.get("skill_name", ""))
                existing_id = existing_skill.get("id", existing_name)
                
                # 정확히 일치하는 경우
                if existing_name == skill_name or existing_id == skill_name:
                    exact_match = existing_skill
                    log(f"🔍 기존 스킬 발견 (정확히 일치): {existing_name} (ID: {existing_id})")
                    break
                
                # 유사한 이름 (공백 제거 후 비교)
                if existing_name.replace(" ", "") == skill_name.replace(" ", ""):
                    if exact_match is None:
                        exact_match = existing_skill
                    else:
                        duplicate_skills.append(existing_skill)
                    log(f"🔍 기존 스킬 발견 (유사한 이름): {existing_name} (ID: {existing_id})")
            
            # 정확히 일치하는 스킬이 있으면 UPDATE로 전환 (HTTP API로 존재 여부 재확인)
            if exact_match:
                matched_id = exact_match.get("id", exact_match.get("name", skill_name))
                
                # HTTP API로 실제 존재 여부 확인 (업로드된 스킬만 UPDATE 가능)
                from core.skill_api_client import check_skill_exists
                try:
                    if not check_skill_exists(matched_id):
                        log(f"   ⚠️ 스킬이 HTTP API에 존재하지 않음 (이미 삭제되었을 수 있음): {matched_id}")
                        # 존재하지 않으면 CREATE로 전환
                        exact_match = None
                    else:
                        log(f"📝 기존 스킬 발견 (HTTP API 검증 완료): {matched_id}. UPDATE 작업으로 전환합니다.")
                        operation = "UPDATE"
                        skill_id = matched_id
                except Exception as e:
                    log(f"   ⚠️ HTTP API 스킬 존재 확인 실패 ({matched_id}): {e}")
                    # 확인 실패 시에도 UPDATE 시도 (이미 verified=True로 필터링했으므로)
                    log(f"📝 기존 스킬 발견: {matched_id}. UPDATE 작업으로 전환합니다.")
                    operation = "UPDATE"
                    skill_id = matched_id
            
            # exact_match가 없어진 경우 (HTTP API에서 존재하지 않음)
            if not exact_match and operation == "UPDATE":
                operation = "CREATE"
                skill_id = None
                log(f"   ℹ️ HTTP API에서 스킬을 찾을 수 없어 CREATE로 전환")
            else:
                # 유사한 스킬이 있는지 충돌 분석 수행 (업로드된 스킬만 대상)
                if existing_skills:
                    new_knowledge = {"skill": skill_artifact}
                    existing_knowledge = {"skills": existing_skills}
                    conflict_result = await analyze_knowledge_conflict(
                        new_knowledge, 
                        existing_knowledge, 
                        "SKILL"
                    )
                    
                    conflict_operation = conflict_result.get("operation", "CREATE")
                    matched_item = conflict_result.get("matched_item")
                    
                    log(f"🔍 스킬 충돌 분석 결과: operation={conflict_operation}, conflict_level={conflict_result.get('conflict_level')}")
                    
                    if conflict_operation == "UPDATE" and matched_item:
                        matched_id = matched_item.get("id")
                        if matched_id:
                            # HTTP API로 실제 존재 여부 확인
                            from core.skill_api_client import check_skill_exists
                            try:
                                if not check_skill_exists(matched_id):
                                    log(f"   ⚠️ 충돌 분석에서 매칭된 스킬이 HTTP API에 존재하지 않음: {matched_id}")
                                    # 존재하지 않으면 CREATE로 유지
                                else:
                                    log(f"📝 충돌 분석 결과 UPDATE (HTTP API 검증 완료): {matched_id}")
                                    operation = "UPDATE"
                                    skill_id = matched_id
                                    
                                    # UPDATE로 전환된 경우, 매칭된 스킬을 exact_match로 설정
                                    for skill in existing_skills:
                                        if skill.get("id") == matched_id or skill.get("name") == matched_id:
                                            exact_match = skill
                                            break
                            except Exception as e:
                                log(f"   ⚠️ HTTP API 스킬 존재 확인 실패 ({matched_id}): {e}")
                                # 확인 실패 시에도 UPDATE 시도
                                log(f"📝 충돌 분석 결과 UPDATE: {matched_id}")
                                operation = "UPDATE"
                                skill_id = matched_id
                                
                                for skill in existing_skills:
                                    if skill.get("id") == matched_id or skill.get("name") == matched_id:
                                        exact_match = skill
                                        break
                    elif conflict_operation == "IGNORE":
                        log(f"⏭️ 충돌 분석 결과 IGNORE: {conflict_result.get('action_description')}")
                        return f"⏭️ 스킬이 무시되었습니다. (이유: {conflict_result.get('conflict_reason', '중복된 스킬')})"
            
            # 중복된 스킬들 처리
            # 1. 정확히 일치하는 스킬과 이름이 같은 다른 스킬들 삭제
            # 2. 충돌 분석 결과 UPDATE로 전환된 경우, 유사한 다른 스킬들도 삭제
            skills_to_delete = []
            
            if exact_match:
                exact_name = exact_match.get("name", exact_match.get("skill_name", ""))
                exact_id = exact_match.get("id", exact_name)
                
                for existing_skill in existing_skills:
                    existing_name = existing_skill.get("name", existing_skill.get("skill_name", ""))
                    existing_id = existing_skill.get("id", existing_name)
                    
                    # 정확히 일치하는 스킬과 이름이 같지만 ID가 다른 경우 (중복)
                    if (existing_name == exact_name or existing_name == skill_name) and existing_id != exact_id:
                        skills_to_delete.append(existing_skill)
            
            # 충돌 분석에서 UPDATE로 전환된 경우, 유사한 다른 스킬들도 확인
            if operation == "UPDATE" and skill_id:
                # 매칭된 스킬의 내용과 유사한 다른 스킬들 찾기
                matched_skill_content = ""
                if exact_match:
                    matched_skill_content = (
                        exact_match.get("content", "") + " " +
                        exact_match.get("description", "") + " " +
                        " ".join(exact_match.get("steps", []))
                    )
                
                # 새 스킬 내용
                new_skill_content = (
                    skill_artifact.get("description", "") + " " +
                    skill_artifact.get("overview", "") + " " +
                    " ".join(skill_artifact.get("steps", []))
                )
                
                # 유사한 스킬 찾기 (간단한 키워드 기반 비교)
                for existing_skill in existing_skills:
                    existing_id = existing_skill.get("id", existing_skill.get("name", ""))
                    if existing_id == skill_id:
                        continue
                    
                    existing_content = (
                        existing_skill.get("content", "") + " " +
                        existing_skill.get("description", "") + " " +
                        " ".join(existing_skill.get("steps", []))
                    )
                    
                    # 간단한 유사도 체크: 공통 키워드가 많으면 유사한 것으로 간주
                    new_keywords = set(new_skill_content.lower().split())
                    existing_keywords = set(existing_content.lower().split())
                    matched_keywords = set(matched_skill_content.lower().split()) if matched_skill_content else set()
                    
                    # 새 스킬과 기존 스킬의 키워드 유사도
                    if new_keywords and existing_keywords:
                        similarity = len(new_keywords & existing_keywords) / max(len(new_keywords), len(existing_keywords))
                        # 유사도가 0.5 이상이면 중복으로 간주
                        if similarity >= 0.5:
                            skills_to_delete.append(existing_skill)
                            log(f"🔍 유사한 스킬 발견 (유사도: {similarity:.2f}): {existing_id}")
            
            # 중복 스킬 삭제 (HTTP API로 실제 존재 여부 확인 후 삭제)
            from core.skill_api_client import check_skill_exists
            for duplicate_skill in skills_to_delete:
                duplicate_id = duplicate_skill.get("id", duplicate_skill.get("name", ""))
                duplicate_name = duplicate_skill.get("name", duplicate_skill.get("skill_name", ""))
                
                # HTTP API로 실제 존재 여부 확인 (업로드된 스킬만 삭제 가능)
                try:
                    if not check_skill_exists(duplicate_id):
                        log(f"   ⚠️ 스킬이 HTTP API에 존재하지 않음 (이미 삭제되었거나 기본 내장 스킬): {duplicate_id}")
                        # 데이터베이스에서만 제거
                        try:
                            update_agent_and_tenant_skills(agent_id, duplicate_id, "DELETE")
                        except Exception as e:
                            log(f"   ⚠️ 데이터베이스 동기화 실패 ({duplicate_id}): {e}")
                        continue
                    
                    log(f"🗑️ 중복 스킬 삭제: {duplicate_id} (이름: {duplicate_name})")
                    delete_result = delete_skill(duplicate_id)
                    log(f"   ✅ 중복 스킬 삭제 완료: {delete_result.get('message', 'Success')}")
                    # 데이터베이스 동기화
                    update_agent_and_tenant_skills(agent_id, duplicate_id, "DELETE")
                except Exception as e:
                    log(f"   ⚠️ 중복 스킬 삭제 실패 ({duplicate_id}): {e}")
        
        # 실제 CRUD 작업 수행
        await commit_to_skill(
            agent_id=agent_id,
            skill_artifact=skill_artifact,
            operation=operation,
            skill_id=skill_id
        )
        
        if operation == "CREATE":
            return f"✅ Skill이 성공적으로 저장되었습니다. (이름: {skill_name}, 에이전트: {agent_id})"
        elif operation == "UPDATE":
            return f"✅ Skill이 성공적으로 수정되었습니다. (ID: {skill_id}, 이름: {skill_name}, 에이전트: {agent_id})"
        elif operation == "DELETE":
            return f"✅ Skill이 성공적으로 삭제되었습니다. (ID: {skill_id}, 에이전트: {agent_id})"
        else:
            return f"⚠️ 알 수 없는 작업: {operation}"
    except Exception as e:
        handle_error("commit_skill_tool", e)
        return f"❌ Skill 저장 실패: {str(e)}"


# ============================================================================
# 새로운 통합 도구 함수 (Phase 2: 의미적 유사도 기반)
# ============================================================================

async def _search_similar_knowledge_tool(
    agent_id: str,
    content: str,
    knowledge_type: str = "ALL",
    threshold: float = 0.7
) -> str:
    """
    모든 저장소에서 의미적으로 유사한 지식을 검색합니다.
    
    Args:
        agent_id: 에이전트 ID
        content: 검색할 지식 내용
        knowledge_type: 검색 대상 타입 (MEMORY | DMN_RULE | SKILL | ALL)
        threshold: 유사도 임계값
    
    Returns:
        유사 지식 검색 결과 (포맷된 텍스트)
    """
    try:
        from core.database import _get_agent_by_id
        
        # 에이전트 정보 조회
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None
        agent_skills = agent_info.get("skills") if agent_info else None
        
        results = []
        matcher = get_semantic_matcher()
        
        # 검색 대상 결정
        search_memory = knowledge_type in ["ALL", "MEMORY"]
        search_dmn = knowledge_type in ["ALL", "DMN_RULE"]
        search_skill = knowledge_type in ["ALL", "SKILL"]
        
        # MEMORY 검색
        if search_memory:
            memories = await retrieve_existing_memories(agent_id, content, limit=20)
            if memories:
                similar_memories = await matcher.find_similar_knowledge(
                    content, memories, "MEMORY", threshold
                )
                for item in similar_memories:
                    item["storage_type"] = "MEMORY"
                results.extend(similar_memories)
        
        # DMN_RULE 검색
        if search_dmn:
            dmn_rules = await retrieve_existing_dmn_rules(agent_id, content[:100])
            if dmn_rules:
                similar_dmn = await matcher.find_similar_knowledge(
                    content, dmn_rules, "DMN_RULE", threshold
                )
                for item in similar_dmn:
                    item["storage_type"] = "DMN_RULE"
                results.extend(similar_dmn)
        
        # SKILL 검색
        if search_skill:
            skills = await retrieve_existing_skills(
                agent_id, content[:100], top_k=20,
                tenant_id=tenant_id, agent_skills=agent_skills
            )
            if skills:
                similar_skills = await matcher.find_similar_knowledge(
                    content, skills, "SKILL", threshold
                )
                for item in similar_skills:
                    item["storage_type"] = "SKILL"
                results.extend(similar_skills)
        
        if not results:
            return f"""관련된 기존 지식이 없습니다. (검색 임계값: {threshold})

이것은 완전히 새로운 지식일 가능성이 높습니다.
새 피드백의 내용을 바탕으로 적절한 저장소(MEMORY/DMN_RULE/SKILL)에 저장할지 판단하세요."""
        
        # 유사도 순으로 정렬
        results.sort(key=lambda x: x.get("similarity_score", 0), reverse=True)
        
        # 관계 유형별 그룹화
        relationship_groups = {}
        for item in results:
            rel = item.get("relationship", "UNKNOWN")
            if rel not in relationship_groups:
                relationship_groups[rel] = []
            relationship_groups[rel].append(item)
        
        # 결과 포맷팅 - 에이전트가 판단할 수 있도록 상세 정보 제공
        output_lines = [f"총 {len(results)}개의 관련 지식을 찾았습니다:\n"]
        
        # 관계 유형 요약
        output_lines.append("📊 관계 유형 분포:")
        for rel_type, items in relationship_groups.items():
            output_lines.append(f"   - {rel_type}: {len(items)}개")
        output_lines.append("")
        
        # 상세 정보
        output_lines.append("📋 상세 분석 결과:")
        for idx, item in enumerate(results[:10], start=1):  # 상위 10개
            storage = item.get("storage_type", "UNKNOWN")
            item_id = item.get("id", "Unknown")
            item_name = item.get("name", item_id)
            score = item.get("similarity_score", 0)
            relationship = item.get("relationship", "UNKNOWN")
            rel_reason = item.get("relationship_reason", "")
            content_summary = item.get("content_summary", "")
            key_diffs = item.get("key_differences", [])
            key_sims = item.get("key_similarities", [])
            full_content = item.get("full_content", "")
            
            output_lines.append(f"\n[{idx}] {item_name}")
            output_lines.append(f"    📁 저장소: {storage}")
            output_lines.append(f"    🔑 ID: {item_id}")
            output_lines.append(f"    🔗 관계 유형: {relationship}")
            output_lines.append(f"    📝 관계 분석: {rel_reason}")
            
            if key_sims:
                output_lines.append(f"    ✅ 유사점: {', '.join(key_sims[:3])}")
            if key_diffs:
                output_lines.append(f"    ❌ 차이점: {', '.join(key_diffs[:3])}")
            
            if content_summary:
                output_lines.append(f"    📄 기존 지식 요약: {content_summary[:200]}...")
            
            # 전체 내용도 일부 포함 (에이전트가 직접 비교 가능)
            if full_content:
                output_lines.append(f"    📜 기존 지식 내용: {full_content[:500]}...")
        
        output_lines.append("")
        output_lines.append("━" * 50)
        output_lines.append("🧠 위 정보를 바탕으로 직접 판단하세요:")
        output_lines.append("   - 이 피드백은 기존 지식과 어떤 관계인가?")
        output_lines.append("   - 기존 지식을 어떻게 처리해야 하나? (유지/수정/삭제/확장)")
        output_lines.append("   - 새 지식을 어떻게 처리해야 하나? (생성/병합/무시)")
        output_lines.append("   - 필요하다면 get_knowledge_detail로 기존 지식의 전체 내용을 확인하세요.")
        
        return "\n".join(output_lines)
        
    except Exception as e:
        handle_error("search_similar_knowledge_tool", e)
        return f"❌ 유사 지식 검색 실패: {str(e)}"


async def _check_duplicate_tool(
    agent_id: str,
    content: str,
    knowledge_type: str,
    candidate_id: Optional[str] = None
) -> str:
    """
    특정 지식이 중복인지 상세 확인합니다.
    
    Args:
        agent_id: 에이전트 ID
        content: 새로운 지식 내용
        knowledge_type: 지식 타입
        candidate_id: 비교할 기존 지식 ID (없으면 가장 유사한 것과 비교)
    
    Returns:
        중복 확인 결과 (포맷된 텍스트)
    """
    try:
        from core.database import _get_agent_by_id
        
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None
        agent_skills = agent_info.get("skills") if agent_info else None
        
        matcher = get_semantic_matcher()
        candidate = None
        
        # 후보 지식 조회
        if candidate_id:
            # 특정 ID로 조회
            if knowledge_type == "MEMORY":
                memories = await retrieve_existing_memories(agent_id, content, limit=50)
                candidate = next((m for m in memories if m.get("id") == candidate_id), None)
            elif knowledge_type == "DMN_RULE":
                dmn_rules = await retrieve_existing_dmn_rules(agent_id, "")
                candidate = next((r for r in dmn_rules if r.get("id") == candidate_id), None)
            elif knowledge_type == "SKILL":
                skills = await retrieve_existing_skills(
                    agent_id, "", top_k=100, tenant_id=tenant_id, agent_skills=agent_skills
                )
                candidate = next((s for s in skills if s.get("id") == candidate_id or s.get("name") == candidate_id), None)
        else:
            # 가장 유사한 항목 찾기
            existing = []
            if knowledge_type == "MEMORY":
                existing = await retrieve_existing_memories(agent_id, content, limit=20)
            elif knowledge_type == "DMN_RULE":
                existing = await retrieve_existing_dmn_rules(agent_id, content[:100])
            elif knowledge_type == "SKILL":
                existing = await retrieve_existing_skills(
                    agent_id, content[:100], top_k=20,
                    tenant_id=tenant_id, agent_skills=agent_skills
                )
            
            if existing:
                similar = await matcher.find_similar_knowledge(content, existing, knowledge_type, 0.5)
                if similar:
                    best = max(similar, key=lambda x: x.get("similarity_score", 0))
                    candidate = best.get("original", existing[0])
        
        if not candidate:
            return f"비교할 기존 지식이 없습니다.\n✅ 권장 작업: CREATE (새로운 지식)"
        
        # 중복 상세 검증
        result = await matcher.verify_duplicate(content, candidate, knowledge_type)
        
        # 결과 포맷팅
        output_lines = [f"중복 검증 결과:\n"]
        output_lines.append(f"비교 대상 ID: {result.get('candidate_id', 'Unknown')}")
        output_lines.append(f"중복 여부: {'예' if result.get('is_duplicate') else '아니오'}")
        output_lines.append(f"신뢰도: {result.get('confidence', 0):.2f}")
        output_lines.append(f"권장 작업: {result.get('recommended_operation', 'CREATE')}")
        output_lines.append(f"판단 이유: {result.get('reason', '')}")
        
        same_aspects = result.get("same_aspects", [])
        if same_aspects:
            output_lines.append(f"\n동일한 부분:")
            for aspect in same_aspects[:5]:
                output_lines.append(f"  - {aspect}")
        
        diff_aspects = result.get("different_aspects", [])
        if diff_aspects:
            output_lines.append(f"\n다른 부분:")
            for aspect in diff_aspects[:5]:
                output_lines.append(f"  - {aspect}")
        
        return "\n".join(output_lines)
        
    except Exception as e:
        handle_error("check_duplicate_tool", e)
        return f"❌ 중복 확인 실패: {str(e)}"


async def _determine_operation_tool(
    agent_id: str,
    content: str,
    knowledge_type: str
) -> str:
    """
    새 지식과 기존 지식의 관계를 분석합니다.
    (작업 결정은 에이전트가 직접 수행)
    
    Args:
        agent_id: 에이전트 ID
        content: 새로운 지식 내용
        knowledge_type: 지식 타입
    
    Returns:
        관계 분석 결과 (에이전트가 판단할 정보 제공)
    """
    try:
        from core.database import _get_agent_by_id
        
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None
        agent_skills = agent_info.get("skills") if agent_info else None
        
        matcher = get_semantic_matcher()
        
        # 기존 지식 조회
        existing = []
        if knowledge_type == "MEMORY":
            existing = await retrieve_existing_memories(agent_id, content, limit=20)
        elif knowledge_type == "DMN_RULE":
            existing = await retrieve_existing_dmn_rules(agent_id, content[:100])
        elif knowledge_type == "SKILL":
            existing = await retrieve_existing_skills(
                agent_id, content[:100], top_k=20,
                tenant_id=tenant_id, agent_skills=agent_skills
            )
        
        if not existing:
            return f"""📊 관계 분석 결과:

기존 {knowledge_type} 지식이 없습니다.

이것은 완전히 새로운 지식으로 보입니다.
피드백 내용을 바탕으로 새 지식을 생성할지 직접 판단하세요."""
        
        # 유사 지식 분석
        similar_items = await matcher.find_similar_knowledge(content, existing, knowledge_type, 0.5)
        
        # 관계 분석 (결정 없이 정보만)
        analysis = await matcher.analyze_relationship(content, similar_items, knowledge_type)
        
        output_lines = ["📊 관계 분석 결과:\n"]
        
        if not analysis.get("has_related_knowledge"):
            output_lines.append("관련된 기존 지식이 없습니다.")
            output_lines.append("새로운 지식으로 판단됩니다.")
        else:
            output_lines.append(f"관련 지식 수: {analysis.get('total_related', 0)}개\n")
            
            # 관계 요약
            rel_summary = analysis.get("relationship_summary", {})
            if rel_summary:
                output_lines.append("📈 관계 유형 분포:")
                for rel_type, count in rel_summary.items():
                    output_lines.append(f"   - {rel_type}: {count}개")
                output_lines.append("")
            
            # 상세 분석
            output_lines.append("📋 상세 분석:")
            output_lines.append(analysis.get("analysis", ""))
            output_lines.append("")
            
            # 관련 지식 상세
            related_items = analysis.get("related_items", [])
            if related_items:
                output_lines.append("🔍 관련 지식 상세:")
                for idx, item in enumerate(related_items[:5], start=1):
                    output_lines.append(f"\n  [{idx}] {item.get('name', item.get('id'))}")
                    output_lines.append(f"      ID: {item.get('id')}")
                    output_lines.append(f"      관계: {item.get('relationship')}")
                    output_lines.append(f"      이유: {item.get('relationship_reason', '')}")
                    
                    key_diffs = item.get("key_differences", [])
                    if key_diffs:
                        output_lines.append(f"      차이점: {', '.join(key_diffs[:3])}")
                    
                    full_content = item.get("full_content", "")
                    if full_content:
                        output_lines.append(f"      내용: {full_content[:300]}...")
        
        output_lines.append("")
        output_lines.append("━" * 50)
        output_lines.append("🧠 위 정보를 바탕으로 직접 판단하세요:")
        output_lines.append("   - DUPLICATE → 저장하지 않음 (IGNORE)")
        output_lines.append("   - EXTENDS → 기존 지식에 새 내용 병합")
        output_lines.append("   - REFINES → 기존 지식의 해당 부분 수정")
        output_lines.append("   - CONFLICTS → 어느 것이 맞는지 판단 필요")
        output_lines.append("   - EXCEPTION → 예외 규칙으로 추가")
        output_lines.append("   - UNRELATED → 새로 생성")
        
        return "\n".join(output_lines)
        
    except Exception as e:
        handle_error("determine_operation_tool", e)
        return f"❌ 관계 분석 실패: {str(e)}"


async def _get_knowledge_detail_tool(
    agent_id: str,
    knowledge_type: str,
    knowledge_id: str
) -> str:
    """
    기존 지식의 전체 상세 내용을 조회합니다.
    에이전트가 기존 지식과 새 피드백을 직접 비교하여 병합 방법을 판단할 수 있도록 합니다.
    
    Args:
        agent_id: 에이전트 ID
        knowledge_type: 지식 타입 (MEMORY | DMN_RULE | SKILL)
        knowledge_id: 조회할 지식 ID
    
    Returns:
        지식의 전체 상세 내용
    """
    try:
        from core.database import _get_agent_by_id
        
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None
        agent_skills = agent_info.get("skills") if agent_info else None
        
        output_lines = [f"📄 {knowledge_type} 상세 조회 결과:\n"]
        
        if knowledge_type == "MEMORY":
            # 빈 쿼리로 semantic search하면 OpenAI API 오류 발생
            # 대신 DB에서 직접 조회
            from core.knowledge_retriever import get_memories_by_agent
            memories = await get_memories_by_agent(agent_id, limit=200)
            target = next((m for m in memories if m.get("id") == knowledge_id), None)
            
            if not target:
                return f"❌ ID가 '{knowledge_id}'인 메모리를 찾을 수 없습니다."
            
            output_lines.append(f"🔑 ID: {target.get('id')}")
            # DB 직접 조회 시 필드명이 다를 수 있음 (memory vs content)
            content = target.get('memory') or target.get('content') or target.get('data', '')
            output_lines.append(f"📝 내용:\n{content}")
            
            metadata = target.get("metadata", {})
            if metadata:
                output_lines.append(f"\n📋 메타데이터:")
                for key, value in metadata.items():
                    output_lines.append(f"   - {key}: {value}")
        
        elif knowledge_type == "DMN_RULE":
            dmn_rules = await retrieve_existing_dmn_rules(agent_id, "")
            target = next((r for r in dmn_rules if r.get("id") == knowledge_id), None)
            
            if not target:
                return f"❌ ID가 '{knowledge_id}'인 DMN 규칙을 찾을 수 없습니다."
            
            output_lines.append(f"🔑 ID: {target.get('id')}")
            output_lines.append(f"📛 이름: {target.get('name', '')}")
            output_lines.append(f"\n📜 DMN XML 전체 내용:")
            output_lines.append("```xml")
            output_lines.append(target.get("bpmn", ""))
            output_lines.append("```")
            
            # XML에서 규칙 정보 추출 시도
            bpmn = target.get("bpmn", "")
            if bpmn:
                import re
                # 간단한 규칙 추출 (inputEntry, outputEntry)
                rules = re.findall(r'<rule[^>]*>.*?</rule>', bpmn, re.DOTALL)
                if rules:
                    output_lines.append(f"\n📊 규칙 수: {len(rules)}개")
        
        elif knowledge_type == "SKILL":
            skills = await retrieve_existing_skills(
                agent_id, "", top_k=100,
                tenant_id=tenant_id, agent_skills=agent_skills
            )
            target = next((s for s in skills if s.get("id") == knowledge_id or s.get("name") == knowledge_id), None)
            
            if not target:
                return f"❌ ID/이름이 '{knowledge_id}'인 스킬을 찾을 수 없습니다."
            
            output_lines.append(f"🔑 ID/이름: {target.get('name', target.get('id'))}")
            output_lines.append(f"📝 설명: {target.get('description', '')}")
            
            content = target.get("content", "")
            if content:
                output_lines.append(f"\n📜 스킬 전체 내용:")
                output_lines.append("```markdown")
                output_lines.append(content)
                output_lines.append("```")
            
            steps = target.get("steps", [])
            if steps:
                output_lines.append(f"\n📋 단계별 절차 ({len(steps)}단계):")
                for idx, step in enumerate(steps, start=1):
                    output_lines.append(f"   {idx}. {step}")
        
        else:
            return f"❌ 지원하지 않는 지식 타입: {knowledge_type}"
        
        output_lines.append("")
        output_lines.append("━" * 50)
        output_lines.append("🧠 이 내용을 바탕으로 피드백과 비교하여 처리 방법을 결정하세요.")
        output_lines.append("   - 병합이 필요하면 기존 내용 + 새 내용을 직접 구성하세요.")
        output_lines.append("   - 수정이 필요하면 변경된 전체 내용을 구성하세요.")
        
        return "\n".join(output_lines)
        
    except Exception as e:
        handle_error("get_knowledge_detail_tool", e)
        return f"❌ 지식 상세 조회 실패: {str(e)}"


# ============================================================================
# LangChain Tool 생성
# ============================================================================

def create_react_tools(agent_id: str) -> List[StructuredTool]:
    """
    ReAct 에이전트용 도구 목록 생성
    
    Args:
        agent_id: 에이전트 ID (도구에 기본값으로 사용)
    
    Returns:
        LangChain Tool 목록
    """
    
    # agent_id를 클로저로 캡처하는 래퍼 함수들
    def search_memory_wrapper(query: str, limit: int = 10) -> str:
        """메모리 검색 도구 (동기 래퍼)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_search_memory_tool(agent_id, query, limit))
    
    def search_dmn_rules_wrapper(search_text: str = "") -> str:
        """DMN 규칙 검색 도구 (동기 래퍼)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_search_dmn_rules_tool(agent_id, search_text))
    
    def search_skills_wrapper(search_text: str = "", top_k: int = 10) -> str:
        """Skills 검색 도구 (동기 래퍼)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_search_skills_tool(agent_id, search_text, top_k))
    
    def analyze_conflict_wrapper(new_knowledge_json: str, existing_knowledge_json: str, target_type: str) -> str:
        """충돌 분석 도구 (동기 래퍼) - JSON 문자열을 파싱하여 딕셔너리로 변환"""
        import asyncio
        import json
        
        def parse_json_input(input_data):
            """JSON 입력을 안전하게 파싱"""
            if isinstance(input_data, dict):
                return input_data
            elif isinstance(input_data, str):
                input_data = input_data.strip()
                if not input_data:
                    raise ValueError("입력이 비어있습니다.")
                
                # 따옴표로 감싸진 문자열인 경우 처리
                if (input_data.startswith("'") and input_data.endswith("'")) or \
                   (input_data.startswith('"') and input_data.endswith('"')):
                    input_data = input_data[1:-1]
                    input_data = input_data.replace("\\'", "'").replace('\\"', '"')
                
                return json.loads(input_data)
            else:
                raise ValueError(f"지원하지 않는 입력 타입: {type(input_data).__name__}")
        
        try:
            # JSON 문자열을 딕셔너리로 파싱
            new_knowledge = parse_json_input(new_knowledge_json)
            existing_knowledge = parse_json_input(existing_knowledge_json)
            
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            return loop.run_until_complete(_analyze_conflict_tool(new_knowledge, existing_knowledge, target_type))
        except (json.JSONDecodeError, ValueError) as e:
            return f"❌ JSON 파싱 실패: {str(e)}\n입력된 new_knowledge_json (첫 500자): {str(new_knowledge_json)[:500]}...\n입력된 existing_knowledge_json (첫 500자): {str(existing_knowledge_json)[:500]}..."
        except Exception as e:
            return f"❌ 충돌 분석 실패: {str(e)}"
    
    def get_knowledge_detail_wrapper(knowledge_type: str, knowledge_id: str = "") -> str:
        """기존 지식 상세 조회 도구 (동기 래퍼) - kwargs 형식 입력 처리"""
        import asyncio
        import re
        
        actual_knowledge_type = knowledge_type
        actual_knowledge_id = knowledge_id
        
        # 에이전트가 kwargs 형식으로 전달한 경우 파싱
        # 예: knowledge_type="DMN_RULE", knowledge_id="customer_benefit_decision"
        if isinstance(knowledge_type, str):
            input_str = knowledge_type.strip()
            
            # kwargs 형식인지 확인
            if 'knowledge_type=' in input_str or 'knowledge_id=' in input_str:
                log(f"🔧 get_knowledge_detail: kwargs 형식 입력 감지, 파싱 시도...")
                log(f"   입력값: {input_str}")
                
                # knowledge_type 추출
                type_match = re.search(r'knowledge_type\s*=\s*["\']?([^"\'",\s]+)["\']?', input_str)
                if type_match:
                    actual_knowledge_type = type_match.group(1)
                    log(f"   추출된 knowledge_type: {actual_knowledge_type}")
                
                # knowledge_id 추출
                id_match = re.search(r'knowledge_id\s*=\s*["\']?([^"\'",\s]+)["\']?', input_str)
                if id_match:
                    actual_knowledge_id = id_match.group(1)
                    log(f"   추출된 knowledge_id: {actual_knowledge_id}")
        
        # knowledge_id가 없으면 에러
        if not actual_knowledge_id:
            return f"❌ knowledge_id가 필요합니다. 입력값: knowledge_type={actual_knowledge_type}"
        
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_get_knowledge_detail_tool(agent_id, actual_knowledge_type, actual_knowledge_id))
    
    def commit_memory_wrapper(content: str, operation: str = "CREATE", memory_id: Optional[str] = None) -> str:
        """메모리 저장 도구 (동기 래퍼)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_commit_memory_tool(agent_id, content, operation, memory_id))
    
    def commit_dmn_rule_wrapper(dmn_artifact_json: str, operation: str = "CREATE", rule_id: Optional[str] = None, feedback_content: str = "") -> str:
        """DMN 규칙 저장 도구 (동기 래퍼) - JSON 문자열을 파싱하여 딕셔너리로 변환"""
        import asyncio
        import json
        import re
        
        # 에이전트가 kwargs 형식으로 전달한 경우 파싱
        # 예: dmn_artifact_json='{"name": "..."}', operation="UPDATE", rule_id="..."
        actual_operation = operation
        actual_rule_id = rule_id
        actual_json = dmn_artifact_json
        
        if isinstance(dmn_artifact_json, str):
            input_str = dmn_artifact_json.strip()
            
            # kwargs 형식인지 확인 (dmn_artifact_json= 또는 operation= 포함)
            if 'dmn_artifact_json=' in input_str or (', operation=' in input_str and ', rule_id=' in input_str):
                log(f"🔧 kwargs 형식 입력 감지, 파싱 시도...")
                
                # operation 추출
                op_match = re.search(r'operation\s*=\s*["\']?(\w+)["\']?', input_str)
                if op_match:
                    actual_operation = op_match.group(1)
                    log(f"   추출된 operation: {actual_operation}")
                
                # rule_id 추출
                rid_match = re.search(r'rule_id\s*=\s*["\']?([^"\'",\s]+)["\']?', input_str)
                if rid_match:
                    actual_rule_id = rid_match.group(1)
                    log(f"   추출된 rule_id: {actual_rule_id}")
                
                # JSON 부분 추출 (중첩 중괄호 처리를 위한 brace counting)
                # 먼저 시작 위치 찾기 (따옴표 포함 가능)
                json_start = -1
                for i, char in enumerate(input_str):
                    if char == '{':
                        # 앞에 따옴표가 있으면 그것부터 시작
                        if i > 0 and input_str[i-1] in "\"'":
                            json_start = i - 1
                        else:
                            json_start = i
                        break
                
                if json_start >= 0:
                    # brace counting으로 끝 위치 찾기
                    brace_count = 0
                    json_end = -1
                    in_string = False
                    escape_next = False
                    actual_start = json_start if input_str[json_start] == '{' else json_start + 1
                    
                    for i in range(actual_start, len(input_str)):
                        char = input_str[i]
                        
                        if escape_next:
                            escape_next = False
                            continue
                        
                        if char == '\\':
                            escape_next = True
                            continue
                        
                        if char == '"' and not in_string:
                            in_string = True
                        elif char == '"' and in_string:
                            in_string = False
                        elif not in_string:
                            if char == '{':
                                brace_count += 1
                            elif char == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    json_end = i + 1
                                    break
                    
                    if json_end > 0:
                        actual_json = input_str[actual_start:json_end]
                        log(f"   추출된 JSON (brace counting): {actual_json[:100]}...")
        
        try:
            # 입력 타입에 따라 처리
            if isinstance(actual_json, dict):
                # 이미 딕셔너리인 경우 그대로 사용
                dmn_artifact = actual_json
            elif isinstance(actual_json, str):
                # 문자열인 경우 파싱 시도
                actual_json = actual_json.strip()
                if not actual_json:
                    return "❌ dmn_artifact_json이 비어있습니다."
                
                # 따옴표로 감싸진 문자열인 경우 처리 (예: '{"name": "..."}')
                if (actual_json.startswith("'") and actual_json.endswith("'")) or \
                   (actual_json.startswith('"') and actual_json.endswith('"')):
                    # 외부 따옴표 제거
                    actual_json = actual_json[1:-1]
                    # 이스케이프된 따옴표 복원
                    actual_json = actual_json.replace("\\'", "'").replace('\\"', '"')
                
                try:
                    dmn_artifact = json.loads(actual_json)
                except json.JSONDecodeError as e:
                    # 파싱 실패 시 더 자세한 에러 정보
                    return f"❌ JSON 파싱 실패: {str(e)}\n입력된 dmn_artifact_json (첫 500자): {actual_json[:500]}...\n입력 타입: {type(actual_json).__name__}"
            else:
                return f"❌ 지원하지 않는 입력 타입: {type(actual_json).__name__}\n입력된 값: {str(actual_json)[:200]}..."
            
            # rules 배열이 있으면 첫 번째 규칙을 사용하거나, 여러 규칙을 하나로 병합
            if "rules" in dmn_artifact and isinstance(dmn_artifact["rules"], list):
                rules = dmn_artifact["rules"]
                if len(rules) > 0:
                    # 첫 번째 규칙의 condition과 action 사용
                    first_rule = rules[0]
                    dmn_artifact = {
                        "name": dmn_artifact.get("name", "피드백 기반 규칙"),
                        "condition": first_rule.get("condition", ""),
                        "action": first_rule.get("action", "")
                    }
                    # 여러 규칙이 있으면 조건과 액션을 병합
                    if len(rules) > 1:
                        conditions = [r.get("condition", "") for r in rules if r.get("condition")]
                        actions = [r.get("action", "") for r in rules if r.get("action")]
                        if conditions:
                            # 여러 조건을 OR로 연결
                            dmn_artifact["condition"] = " 또는 ".join([f"({c})" for c in conditions if c])
                        if actions:
                            # 여러 액션을 세미콜론으로 연결
                            dmn_artifact["action"] = "; ".join(actions)
                    log(f"⚠️ rules 배열에서 변환: {len(rules)}개 규칙을 하나로 병합")
                else:
                    return "❌ rules 배열이 비어있습니다."
            
            # condition과 action이 있는지 확인
            if not dmn_artifact.get("condition") or not dmn_artifact.get("action"):
                return f"❌ condition과 action이 필요합니다. 전달된 데이터: {json.dumps(dmn_artifact, ensure_ascii=False)[:200]}..."
            
            # 추출된 operation/rule_id 로깅
            log(f"📋 DMN 규칙 저장 호출: operation={actual_operation}, rule_id={actual_rule_id}")
            
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # ⚠️ 자동 확장 로직 제거: 에이전트가 직접 판단하여 완성된 내용을 전달해야 함
            # 병합이 필요하면 에이전트가 get_knowledge_detail로 기존 내용을 조회하고 직접 구성
            return loop.run_until_complete(_commit_dmn_rule_tool(agent_id, dmn_artifact, actual_operation, actual_rule_id, feedback_content))
        except json.JSONDecodeError as e:
            return f"❌ JSON 파싱 실패: {str(e)}\n입력된 dmn_artifact_json: {actual_json[:200] if isinstance(actual_json, str) else str(actual_json)[:200]}..."
        except Exception as e:
            return f"❌ DMN 규칙 저장 실패: {str(e)}"
    
    def commit_skill_wrapper(skill_artifact_json: str, operation: str = "CREATE", skill_id: Optional[str] = None) -> str:
        """Skill 저장 도구 (동기 래퍼) - JSON 문자열을 파싱하여 딕셔너리로 변환"""
        import asyncio
        import json
        
        try:
            # 입력 타입에 따라 처리
            if isinstance(skill_artifact_json, dict):
                skill_artifact = skill_artifact_json
            elif isinstance(skill_artifact_json, str):
                skill_artifact_json = skill_artifact_json.strip()
                if not skill_artifact_json:
                    return "❌ skill_artifact_json이 비어있습니다."
                
                # 따옴표로 감싸진 문자열인 경우 처리
                if (skill_artifact_json.startswith("'") and skill_artifact_json.endswith("'")) or \
                   (skill_artifact_json.startswith('"') and skill_artifact_json.endswith('"')):
                    skill_artifact_json = skill_artifact_json[1:-1]
                    skill_artifact_json = skill_artifact_json.replace("\\'", "'").replace('\\"', '"')
                
                try:
                    skill_artifact = json.loads(skill_artifact_json)
                except json.JSONDecodeError as e:
                    return f"❌ JSON 파싱 실패: {str(e)}\n입력된 skill_artifact_json (첫 500자): {skill_artifact_json[:500]}..."
            else:
                return f"❌ 지원하지 않는 입력 타입: {type(skill_artifact_json).__name__}"
            
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            return loop.run_until_complete(_commit_skill_tool(agent_id, skill_artifact, operation, skill_id))
        except json.JSONDecodeError as e:
            return f"❌ JSON 파싱 실패: {str(e)}\n입력된 skill_artifact_json: {skill_artifact_json[:200]}..."
        except Exception as e:
            return f"❌ Skill 저장 실패: {str(e)}"
    
    # 새로운 통합 도구 래퍼 함수들
    def search_similar_knowledge_wrapper(content: str, knowledge_type: str = "ALL", threshold: float = 0.7) -> str:
        """유사 지식 검색 도구 (동기 래퍼)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_search_similar_knowledge_tool(agent_id, content, knowledge_type, threshold))
    
    def check_duplicate_wrapper(content: str, knowledge_type: str, candidate_id: Optional[str] = None) -> str:
        """중복 확인 도구 (동기 래퍼)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_check_duplicate_tool(agent_id, content, knowledge_type, candidate_id))
    
    def determine_operation_wrapper(content: str, knowledge_type: str) -> str:
        """작업 결정 도구 (동기 래퍼)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_determine_operation_tool(agent_id, content, knowledge_type))
    
    tools = [
        StructuredTool.from_function(
            func=search_memory_wrapper,
            name="search_memory",
            description="mem0에서 관련 메모리를 검색합니다. 피드백 내용과 유사한 기존 지식을 찾을 때 사용합니다.",
            args_schema=SearchMemoryInput
        ),
        StructuredTool.from_function(
            func=search_dmn_rules_wrapper,
            name="search_dmn_rules",
            description="DMN 규칙을 검색합니다. 조건-결과 형태의 비즈니스 판단 규칙을 찾을 때 사용합니다.",
            args_schema=SearchDmnRulesInput
        ),
        StructuredTool.from_function(
            func=search_skills_wrapper,
            name="search_skills",
            description="Skills를 검색합니다. 반복 가능한 절차나 작업 순서를 찾을 때 사용합니다.",
            args_schema=SearchSkillsInput
        ),
        # 새로운 통합 도구들 (의미적 유사도 기반)
        StructuredTool.from_function(
            func=search_similar_knowledge_wrapper,
            name="search_similar_knowledge",
            description="""모든 저장소에서 의미적으로 유사한 기존 지식을 검색하고 관계를 분석합니다.
피드백을 저장하기 전에 반드시 이 도구를 먼저 사용하세요.
검색 결과에서 관계 유형(DUPLICATE, EXTENDS, REFINES, CONFLICTS 등)을 확인하고,
기존 지식과 새 피드백의 관계를 직접 분석하여 처리 방법을 결정하세요.""",
            args_schema=SearchSimilarKnowledgeInput
        ),
        StructuredTool.from_function(
            func=check_duplicate_wrapper,
            name="check_duplicate",
            description="""특정 지식이 기존 지식과 중복인지 상세 확인합니다.
search_similar_knowledge로 유사한 지식을 찾은 후, 정확한 중복 여부를 확인할 때 사용합니다.""",
            args_schema=CheckDuplicateInput
        ),
        StructuredTool.from_function(
            func=determine_operation_wrapper,
            name="determine_operation",
            description="""새 지식과 기존 지식의 관계를 분석하여 정보를 제공합니다.
관계 유형(DUPLICATE, EXTENDS, REFINES, CONFLICTS 등)과 상세 분석 결과를 반환합니다.
⚠️ 이 도구는 작업을 결정하지 않습니다. 제공된 정보를 바탕으로 직접 판단하세요.""",
            args_schema=DetermineOperationInput
        ),
        StructuredTool.from_function(
            func=get_knowledge_detail_wrapper,
            name="get_knowledge_detail",
            description="""기존 지식의 전체 상세 내용을 조회합니다.
기존 지식과 새 피드백을 직접 비교하여 병합/수정 방법을 결정할 때 사용합니다.
DMN 규칙의 경우 전체 XML을, SKILL의 경우 전체 steps를 반환합니다.
병합이 필요하면 이 도구로 기존 내용을 조회한 후 직접 합쳐서 저장하세요.""",
            args_schema=GetKnowledgeDetailInput
        ),
        StructuredTool.from_function(
            func=commit_memory_wrapper,
            name="commit_to_memory",
            description="mem0에 메모리를 저장/수정/삭제합니다. 지침, 선호도, 맥락 정보를 저장할 때 사용합니다.",
            args_schema=CommitMemoryInput
        ),
        StructuredTool.from_function(
            func=commit_dmn_rule_wrapper,
            name="commit_to_dmn_rule",
            description="""DMN 규칙을 저장/수정/삭제합니다.

⚠️ 중요: 유사한 기존 규칙이 있으면 반드시 operation="UPDATE"와 rule_id를 함께 전달하세요!
- CREATE (기본값): 새 규칙 생성. 유사 규칙이 없을 때만 사용
- UPDATE: 기존 규칙 수정. 반드시 rule_id 필수!
- DELETE: 기존 규칙 삭제. 반드시 rule_id 필수!

예시 (UPDATE): dmn_artifact_json='{"name": "규칙명", "condition": "조건", "action": "결과"}', operation="UPDATE", rule_id="기존_규칙_ID"
예시 (CREATE): dmn_artifact_json='{"name": "규칙명", "condition": "조건", "action": "결과"}'""",
            args_schema=CommitDmnRuleInput
        ),
        StructuredTool.from_function(
            func=commit_skill_wrapper,
            name="commit_to_skill",
            description="Skill을 저장/수정/삭제합니다. 반복 가능한 절차나 작업 순서를 저장할 때 사용합니다. skill_artifact_json은 JSON 문자열 형식으로 전달해야 합니다 (예: '{\"name\": \"스킬 이름\", \"steps\": [\"1단계\", \"2단계\", ...]}').",
            args_schema=CommitSkillInput
        ),
    ]
    
    return tools

