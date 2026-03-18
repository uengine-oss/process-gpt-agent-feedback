"""
ReAct 에이전트용 도구 정의
기존 함수들을 LangChain Tool로 래핑
"""

import json
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from utils.logger import log, handle_error

# Pydantic v2 model_validator를 위한 import
try:
    from pydantic import model_validator
except ImportError:
    # Pydantic v1 호환성
    try:
        from pydantic import root_validator as model_validator
        # v1에서는 mode 인자를 사용하지 않으므로 wrapper 함수 필요
        def _model_validator_wrapper(mode='before'):
            def decorator(func):
                if mode == 'before':
                    return model_validator(pre=True)(func)
                return func
            return decorator
        model_validator = _model_validator_wrapper
    except ImportError:
        model_validator = None

# 기존 모듈 import
from core.knowledge_retriever import (
    retrieve_existing_memories,
    retrieve_existing_dmn_rules,
    retrieve_existing_skills,
    retrieve_all_existing_knowledge,
    SKILL_SEARCH_CONTEXT_CHARS,
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
    merge_mode: Optional[str] = Field(default="REPLACE", description="병합 모드 (REPLACE | EXTEND | REFINE). EXTEND: 기존 규칙 보존 + 새 규칙 추가. REFINE: 기존 규칙 참조 후 일부 수정. REPLACE: 완전 대체 (기본값)")


class AttachSkillsToAgentInput(BaseModel):
    """기존 스킬을 에이전트에 적재하는 도구 입력 (스킬 생성/수정 없음)"""
    skill_ids: str = Field(
        ...,
        description="에이전트에 적재할 기존 스킬 이름/ID를 쉼표 구분 (예: 'skill-a, skill-b'). search_similar_knowledge에서 찾은 스킬 ID 사용."
    )


class CommitSkillInput(BaseModel):
    """Skill 저장 도구 입력

    ReAct은 **어떤 지식 저장소에(SKILL)**·**기존 지식과의 관계(CREATE/UPDATE/DELETE, skill_id)**만 판단합니다.
    스킬 마크다운·steps·additional_files 등 **스킬 내용 생성은 전부 skill-creator 스킬**이 수행합니다.
    피드백(feedback_content)은 도구 외부에서 자동 전달됩니다.
    """
    operation: str = Field(
        default="CREATE",
        description="작업 타입 (CREATE | UPDATE | DELETE). 관련 스킬이 있으면 UPDATE, 없으면 CREATE."
    )
    skill_id: Optional[str] = Field(
        default=None,
        description="UPDATE/DELETE 시 필수. 기존 스킬 이름(id). CREATE 시에는 비워둠."
    )
    merge_mode: Optional[str] = Field(
        default="MERGE",
        description="UPDATE 시 병합 모드 (MERGE | REPLACE). MERGE: 기존 보존+변경 반영. REPLACE: 전체 교체.",
    )
    relationship_analysis: Optional[str] = Field(
        default=None,
        description="search_similar_knowledge 결과(관계 유형 분포·상세 분석)를 그대로 전달. EXTENDS/COMPLEMENTS 시 기존 내용 보존에 활용. 있으면 반드시 전달하세요.",
    )
    related_skill_ids: Optional[str] = Field(
        default=None,
        description="search_similar_knowledge에서 찾은 관련 스킬 이름/ID를 쉼표 구분 문자열로 전달 (예: 'skill-a, skill-b'). 있으면 전달하면 스킬 간 참조 생성에 활용됩니다.",
    )


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
    
    if model_validator:
        @model_validator(mode='before')
        @classmethod
        def parse_kwargs_input(cls, data):
            """kwargs 형식 입력을 처리하는 validator"""
            if isinstance(data, dict):
                # content 필드에 kwargs 형식 문자열이 들어있는 경우 파싱
                if 'content' in data and isinstance(data['content'], str):
                    content_value = data['content']
                    if 'knowledge_type=' in content_value:
                        log(f"🔧 DetermineOperationInput: kwargs 형식 입력 감지, 파싱 시도...")
                        log(f"   입력값: {content_value[:200]}...")
                        
                        # content 추출
                        content_match = re.search(r'content\s*=\s*["\']([^"\']*)["\']', content_value)
                        if content_match:
                            data['content'] = content_match.group(1)
                            log(f"   추출된 content: {data['content'][:100]}...")
                        else:
                            # content=...knowledge_type= 형태에서 content 부분만 추출
                            content_end = content_value.find('knowledge_type=')
                            if content_end > 0:
                                content_part = content_value[:content_end].strip()
                                if content_part.startswith('content='):
                                    data['content'] = content_part[8:].strip().strip("'\"")
                                    log(f"   추출된 content (후처리): {data['content'][:100]}...")
                        
                        # knowledge_type 추출 (이미 딕셔너리에 있으면 덮어쓰지 않음)
                        if 'knowledge_type' not in data or not data.get('knowledge_type'):
                            type_match = re.search(r'knowledge_type\s*=\s*["\']?([^"\'",\s]+)["\']?', content_value)
                            if type_match:
                                data['knowledge_type'] = type_match.group(1)
                                log(f"   추출된 knowledge_type: {data['knowledge_type']}")
            
            return data


class GetKnowledgeDetailInput(BaseModel):
    """기존 지식 상세 조회 도구 입력"""
    knowledge_type: str = Field(
        default="AUTO",
        description="지식 타입 (MEMORY | DMN_RULE | SKILL | AUTO). AUTO면 ID로 모든 타입에서 조회를 시도합니다.",
    )
    knowledge_id: str = Field(default="", description="조회할 지식 ID/이름 (필수). ReAct 텍스트 에이전트의 경우 JSON이 문자열로 들어올 수 있어 도구에서 복구합니다.")


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
    feedback_content: str = "",
    merge_mode: str = "REPLACE"
) -> str:
    """
    DMN 규칙을 저장/수정/삭제합니다.
    
    Args:
        agent_id: 에이전트 ID
        dmn_artifact: DMN 규칙 정보 (name, condition, action 포함)
        operation: CREATE | UPDATE | DELETE
        rule_id: UPDATE/DELETE 시 기존 규칙 ID
        feedback_content: 원본 피드백 내용 (선택적)
        merge_mode: REPLACE | EXTEND | REFINE (기본값: REPLACE)
    
    Returns:
        작업 결과 메시지
    """
    try:
        # dmn_artifact를 완전히 정규화하는 함수 (재귀적으로 condition/action 추출)
        def normalize_dmn_artifact(obj):
            """dmn_artifact를 정규화하여 condition, action, name을 확실히 추출"""
            if not isinstance(obj, dict):
                return obj
            
            # 이미 condition과 action이 최상위에 있으면 그대로 사용
            if "condition" in obj and "action" in obj:
                condition = obj.get("condition", "")
                action = obj.get("action", "")
                if condition and action and isinstance(condition, str) and condition.strip() and isinstance(action, str) and action.strip():
                    return {
                        # 이름이 없으면 나중 단계에서 안전하게 기본값을 적용
                        "name": obj.get("name"),
                        "condition": condition,
                        "action": action
                    }
            
            # 중첩된 dmn_artifact_json에서 찾기
            if "dmn_artifact_json" in obj:
                nested = normalize_dmn_artifact(obj["dmn_artifact_json"])
                if isinstance(nested, dict) and "condition" in nested and "action" in nested:
                    return nested
            
            # rules 배열에서 찾기
            if "rules" in obj and isinstance(obj.get("rules"), list):
                rules = obj["rules"]
                if len(rules) > 0:
                    first_rule = rules[0] if isinstance(rules[0], dict) else {}
                    condition = first_rule.get("condition") or first_rule.get("input", "")
                    action = first_rule.get("action") or first_rule.get("output", "")
                    if condition and action:
                        # 여러 규칙이 있으면 병합
                        if len(rules) > 1:
                            conditions = []
                            actions = []
                            for r in rules:
                                if isinstance(r, dict):
                                    c = r.get("condition") or r.get("input", "")
                                    a = r.get("action") or r.get("output", "")
                                    if c:
                                        conditions.append(c)
                                    if a:
                                        actions.append(a)
                            if conditions and actions:
                                merged_condition = " 또는 ".join([f"({c})" for c in conditions if c])
                                merged_action = "; ".join([a for a in actions if a])
                                return {
                                    "name": obj.get("name"),
                                    "condition": merged_condition,
                                    "action": merged_action
                                }
                        if condition and action:
                            return {
                                "name": obj.get("name"),
                                "condition": condition,
                                "action": action
                            }
            
            # 그 외의 경우 원본 반환 (하지만 condition/action이 없으면 문제)
            return obj
        
        # dmn_artifact 정규화
        normalized_artifact = normalize_dmn_artifact(dmn_artifact)
        
        # 정규화 후에도 condition과 action이 없으면 에러
        if not isinstance(normalized_artifact, dict) or not normalized_artifact.get("condition") or not normalized_artifact.get("action"):
            log(f"⚠️ _commit_dmn_rule_tool: 정규화 후에도 condition/action을 찾을 수 없음")
            try:
                log(f"   원본 dmn_artifact: {json.dumps(dmn_artifact, ensure_ascii=False, indent=2)}")
                log(f"   정규화된 artifact: {json.dumps(normalized_artifact, ensure_ascii=False, indent=2)}")
            except Exception:
                log(f"   원본 dmn_artifact: {str(dmn_artifact)[:500]}")
                log(f"   정규화된 artifact: {str(normalized_artifact)[:500]}")
            return f"❌ DMN 규칙 저장 실패: condition과 action을 추출할 수 없습니다. 전달된 데이터 구조를 확인해주세요."
        
        log(f"✅ _commit_dmn_rule_tool: 정규화 완료 - condition={normalized_artifact.get('condition', '')[:50]}..., action={normalized_artifact.get('action', '')[:50]}...")
        
        await commit_to_dmn_rule(
            agent_id=agent_id,
            dmn_artifact=normalized_artifact,
            feedback_content=feedback_content,
            operation=operation,
            rule_id=rule_id,
            merge_mode=merge_mode
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
    operation: str = "CREATE",
    skill_id: Optional[str] = None,
    merge_mode: str = "MERGE",
    feedback_content: Optional[str] = None,
    relationship_analysis: Optional[str] = None,
    related_skill_ids: Optional[str] = None,
) -> str:
    """
    Skill을 저장/수정/삭제합니다. ReAct은 저장소·관계(operation, skill_id)만 판단하고,
    스킬 내용(SKILL.md, steps, additional_files)은 skill-creator가 생성합니다.
    """
    try:
        if operation == "DELETE":
            if not skill_id or not str(skill_id).strip():
                return "❌ DELETE에는 skill_id(기존 스킬 이름)가 필요합니다."
        elif operation == "UPDATE":
            if not skill_id or not str(skill_id).strip():
                return "❌ UPDATE에는 skill_id(기존 스킬 이름)가 필요합니다."
        elif operation == "CREATE":
            if not feedback_content or not str(feedback_content).strip():
                return "❌ CREATE에는 피드백이 필요합니다. (skill-creator가 피드백을 바탕으로 스킬을 생성합니다.)"

        await commit_to_skill(
            agent_id=agent_id,
            skill_artifact=None,
            operation=operation,
            skill_id=skill_id,
            merge_mode=merge_mode,
            feedback_content=feedback_content or "",
            relationship_analysis=relationship_analysis,
            related_skill_ids=related_skill_ids,
        )

        if operation == "CREATE":
            return f"✅ Skill이 성공적으로 저장되었습니다. (skill-creator가 생성, 에이전트: {agent_id})"
        if operation == "UPDATE":
            return f"✅ Skill이 성공적으로 수정되었습니다. (ID: {skill_id}, 에이전트: {agent_id})"
        if operation == "DELETE":
            return f"✅ Skill이 성공적으로 삭제되었습니다. (ID: {skill_id}, 에이전트: {agent_id})"
        return f"⚠️ 알 수 없는 작업: {operation}"
    except Exception as e:
        handle_error("commit_skill_tool", e)
        return f"❌ Skill 저장 실패: {str(e)}"


def _parse_skill_ids_input(skill_ids: Any) -> List[str]:
    """skill_ids 입력을 파싱하여 스킬명 리스트 반환. JSON/dict/문자열 모두 처리."""
    raw = skill_ids
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("skill_ids", "") or ""
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                raw = obj.get("skill_ids", "") or ""
            elif isinstance(obj, list):
                raw = ",".join(str(x) for x in obj)
        except (json.JSONDecodeError, TypeError):
            pass
    return [s.strip() for s in str(raw).split(",") if s.strip()]


async def _attach_skills_to_agent_tool(agent_id: str, skill_ids: Any) -> str:
    """
    기존 스킬을 에이전트에 적재만 합니다. 스킬 내용은 생성/수정하지 않습니다.
    유사도가 높은 기존 스킬로 요구사항을 충족할 때 사용합니다.

    Args:
        agent_id: 에이전트 ID
        skill_ids: 쉼표 구분 스킬 이름/ID (예: 'skill-a, skill-b') 또는 JSON {"skill_ids": "..."}

    Returns:
        처리 결과 메시지
    """
    try:
        from core.database import (
            _get_agent_by_id,
            update_agent_and_tenant_skills,
            register_knowledge,
            record_knowledge_history,
        )
        from utils.logger import log

        skill_names = _parse_skill_ids_input(skill_ids)
        if not skill_names:
            return "❌ skill_ids가 비어있습니다. 쉼표 구분으로 스킬 이름을 입력하세요."

        agent_info = _get_agent_by_id(agent_id)
        if not agent_info:
            return f"❌ 에이전트를 찾을 수 없습니다: {agent_id}"
        tenant_id = agent_info.get("tenant_id")

        attached = []
        for skill_name in skill_names[:10]:  # 최대 10개
            try:
                update_agent_and_tenant_skills(agent_id, skill_name, "CREATE")
                register_knowledge(
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    knowledge_type="SKILL",
                    knowledge_id=skill_name,
                    knowledge_name=skill_name,
                    content_summary=f"기존 스킬 적재: {skill_name}",
                )
                record_knowledge_history(
                    knowledge_type="SKILL",
                    knowledge_id=skill_name,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="CREATE",
                    new_content={"source": "attach_existing_skill", "skill_name": skill_name},
                    feedback_content=None,
                    knowledge_name=skill_name,
                )
                attached.append(skill_name)
                log(f"✅ 스킬 에이전트 적재 완료: {skill_name} (agent_id={agent_id})")
            except Exception as e:
                log(f"⚠️ 스킬 적재 실패 ({skill_name}): {e}")
                # 계속 진행

        if not attached:
            return f"❌ 스킬 적재 실패: {', '.join(skill_names)}"
        return f"✅ 기존 스킬 {len(attached)}개를 에이전트에 적재했습니다: {', '.join(attached)}"
    except Exception as e:
        handle_error("attach_skills_to_agent_tool", e)
        return f"❌ 스킬 적재 실패: {str(e)}"


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
    레지스트리를 먼저 조회하고, 없으면 기존 방식으로 계산 후 저장합니다.
    
    Args:
        agent_id: 에이전트 ID
        content: 검색할 지식 내용
        knowledge_type: 검색 대상 타입 (MEMORY | DMN_RULE | SKILL | ALL)
        threshold: 유사도 임계값
    
    Returns:
        유사 지식 검색 결과 (포맷된 텍스트)
    """
    try:
        from core.database import (
            _get_agent_by_id,
            get_agent_knowledge_list,
            register_knowledge,
            update_knowledge_access_time
        )
        from utils.logger import log
        
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
        
        # 1단계: 피드백과 직접 유사한 지식 찾기
        # MEMORY 검색
        if search_memory:
            memories = await retrieve_existing_memories(agent_id, content, limit=20)
            if memories:
                similar_memories = await matcher.find_similar_knowledge(
                    content, memories, "MEMORY", threshold
                )
                for item in similar_memories:
                    item["storage_type"] = "MEMORY"
                    results.append(item)
                    
                    # 레지스트리에 등록 및 접근 시간 업데이트
                    try:
                        register_knowledge(
                            agent_id=agent_id,
                            tenant_id=tenant_id,
                            knowledge_type="MEMORY",
                            knowledge_id=item.get("id", ""),
                            knowledge_name=item.get("name", ""),
                            content_summary=item.get("content_summary", ""),
                            content=item.get("full_content", "")
                        )
                        update_knowledge_access_time(agent_id, "MEMORY", item.get("id", ""))
                    except Exception as e:
                        log(f"⚠️ 레지스트리 등록 실패 (무시하고 계속 진행): {e}")
        
        # DMN_RULE 검색
        if search_dmn:
            dmn_rules = await retrieve_existing_dmn_rules(agent_id, content[:100])
            if dmn_rules:
                similar_dmn = await matcher.find_similar_knowledge(
                    content, dmn_rules, "DMN_RULE", threshold
                )
                for item in similar_dmn:
                    item["storage_type"] = "DMN_RULE"
                    results.append(item)
                    
                    # 레지스트리에 등록 및 접근 시간 업데이트
                    try:
                        register_knowledge(
                            agent_id=agent_id,
                            tenant_id=tenant_id,
                            knowledge_type="DMN_RULE",
                            knowledge_id=item.get("id", ""),
                            knowledge_name=item.get("name", ""),
                            content_summary=item.get("content_summary", "")
                        )
                        update_knowledge_access_time(agent_id, "DMN_RULE", item.get("id", ""))
                    except Exception as e:
                        log(f"⚠️ 레지스트리 등록 실패 (무시하고 계속 진행): {e}")
        
        # SKILL 검색
        if search_skill:
            skills = await retrieve_existing_skills(
                agent_id, content[:SKILL_SEARCH_CONTEXT_CHARS], top_k=20,
                tenant_id=tenant_id, agent_skills=agent_skills
            )
            if skills:
                similar_skills = await matcher.find_similar_knowledge(
                    content, skills, "SKILL", threshold
                )
                for item in similar_skills:
                    item["storage_type"] = "SKILL"
                    # 숫자/인덱스 형태 ID는 레지스트리에 등록하지 않음 (phantom SKILL:1 방지)
                    skill_id = item.get("id", "")
                    skill_name = item.get("name", "") or item.get("skill_name", "")
                    if isinstance(skill_id, int) or (isinstance(skill_id, str) and skill_id.isdigit()) or (
                        isinstance(skill_id, str) and skill_id.startswith("skill_") and len(skill_id) > 6 and skill_id[6:].isdigit()
                    ):
                        skill_id = skill_name or str(skill_id)
                    if not skill_id:
                        continue
                    item["id"] = skill_id
                    results.append(item)
                    
                    # 레지스트리에 등록 및 접근 시간 업데이트
                    try:
                        register_knowledge(
                            agent_id=agent_id,
                            tenant_id=tenant_id,
                            knowledge_type="SKILL",
                            knowledge_id=skill_id,
                            knowledge_name=skill_name or skill_id,
                            content_summary=item.get("content_summary", "")
                        )
                        update_knowledge_access_time(agent_id, "SKILL", skill_id)
                    except Exception as e:
                        log(f"⚠️ 레지스트리 등록 실패 (무시하고 계속 진행): {e}")
        
        # 2단계: 레지스트리에서 관련 지식 추가 조회 (선택적)
        # 찾은 지식이 적을 경우 레지스트리에서 유사한 지식 이름으로 검색
        if len(results) < 5:
            try:
                registry_knowledge = get_agent_knowledge_list(
                    agent_id=agent_id,
                    knowledge_type=knowledge_type if knowledge_type != "ALL" else None,
                    limit=50
                )
                
                # 레지스트리의 지식 이름이나 요약에서 피드백 내용과 유사한 것 찾기
                for reg_item in registry_knowledge:
                    reg_name = reg_item.get("knowledge_name", "")
                    reg_summary = reg_item.get("content_summary", "")
                    
                    # 간단한 키워드 매칭 (더 정교한 검색은 필요시 추가)
                    if reg_name and content.lower() in reg_name.lower():
                        # 이미 결과에 있는지 확인
                        existing = any(
                            r.get("storage_type") == reg_item.get("knowledge_type") and
                            r.get("id") == reg_item.get("knowledge_id")
                            for r in results
                        )
                        if not existing:
                            results.append({
                                "id": reg_item.get("knowledge_id", ""),
                                "name": reg_item.get("knowledge_name", ""),
                                "storage_type": reg_item.get("knowledge_type", ""),
                                "similarity_score": 0.6,  # 레지스트리에서 찾은 경우 기본 점수
                                "relationship": "RELATED",
                                "relationship_reason": "레지스트리에서 이름 매칭으로 발견",
                                "from_registry": True
                            })
            except Exception as e:
                log(f"⚠️ 레지스트리 추가 조회 실패 (무시하고 계속 진행): {e}")
        
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
        
        # 상세 정보 (SKILL은 표시용 ID가 숫자/인덱스 형태면 name 사용)
        output_lines.append("📋 상세 분석 결과:")
        for idx, item in enumerate(results[:10], start=1):  # 상위 10개
            storage = item.get("storage_type", "UNKNOWN")
            item_id = item.get("id", "Unknown")
            item_name = item.get("name", item_id)
            if storage == "SKILL" and item_id != item_name:
                sid = item_id
                if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()) or (
                    isinstance(sid, str) and len(sid) > 6 and sid.startswith("skill_") and sid[6:].isdigit()
                ):
                    item_id = item_name or str(sid)
                    item["id"] = item_id
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
        
        # SKILL 재사용 가이드: 유사도 높고 DUPLICATE/COMPLEMENTS면 attach_skills_to_agent 권장
        skill_results = [r for r in results if r.get("storage_type") == "SKILL"]
        high_sim_skills = [
            r for r in skill_results
            if r.get("similarity_score", 0) >= 0.85
            and r.get("relationship", "") in ("DUPLICATE", "COMPLEMENTS", "EXTENDS")
        ]
        if high_sim_skills:
            skill_ids = [r.get("id", "") or r.get("name", "") for r in high_sim_skills[:5] if r.get("id") or r.get("name")]
            if skill_ids:
                output_lines.append("")
                output_lines.append("📌 **SKILL 재사용 권장:**")
                output_lines.append(f"   유사도 0.85 이상 + DUPLICATE/COMPLEMENTS/EXTENDS 관계인 스킬 {len(skill_ids)}개 발견.")
                output_lines.append("   기존 스킬로 요구사항을 충분히 충족하면 **attach_skills_to_agent** 사용 권장 (새 스킬 생성 대신).")
                output_lines.append(f"   예: attach_skills_to_agent(skill_ids=\"{', '.join(skill_ids)}\")")

        output_lines.append("")
        output_lines.append("━" * 50)
        output_lines.append("🧠 위 정보를 바탕으로 직접 판단하세요:")
        output_lines.append("   - 이 피드백은 기존 지식과 어떤 관계인가?")
        output_lines.append("   - 기존 지식을 어떻게 처리해야 하나? (유지/수정/삭제/확장/적재)")
        output_lines.append("   - **SKILL:** 목표/결과(예: 의사결정 기여)는 스킬 절차가 아님. 구체적 절차·산출물이 기존 스킬로 커버되면 attach_skills_to_agent 우선, 새 절차가 필요할 때만 commit_to_skill.")
        output_lines.append("   - 새 지식을 어떻게 처리해야 하나? (생성/병합/무시/기존 적재)")
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
                # 직접 조회: 업로드 스킬(HTTP API) → 내장 스킬(MCP) 순서 (벡터 검색 100개 회피)
                candidate = None
                _cand_name = candidate_id
                try:
                    from core.skill_api_client import check_skill_exists_with_info as _check_skill, get_skill_file_content as _get_skill_file
                    _info = _check_skill(_cand_name)
                    if _info and _info.get("exists"):
                        _file = _get_skill_file(_cand_name, "SKILL.md")
                        candidate = {
                            "id": _cand_name, "name": _info.get("name", _cand_name),
                            "description": _info.get("description", ""),
                            "content": _file.get("content", ""),
                        }
                except Exception:
                    pass
                if not candidate:
                    from core.knowledge_retriever import _read_skill_document_mcp
                    _content = await _read_skill_document_mcp(_cand_name, "SKILL.md")
                    if _content:
                        candidate = {"id": _cand_name, "name": _cand_name, "description": "", "content": _content}
        else:
            # 가장 유사한 항목 찾기
            existing = []
            if knowledge_type == "MEMORY":
                existing = await retrieve_existing_memories(agent_id, content, limit=20)
            elif knowledge_type == "DMN_RULE":
                existing = await retrieve_existing_dmn_rules(agent_id, content[:100])
            elif knowledge_type == "SKILL":
                existing = await retrieve_existing_skills(
                    agent_id, content[:SKILL_SEARCH_CONTEXT_CHARS], top_k=20,
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
                agent_id, content[:SKILL_SEARCH_CONTEXT_CHARS], top_k=20,
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
        
        if knowledge_type == "AUTO":
            # AUTO: 순차적으로 조회 시도 (가장 흔한 SKILL → DMN_RULE → MEMORY)
            for t in ["SKILL", "DMN_RULE", "MEMORY"]:
                try:
                    result = await _get_knowledge_detail_tool(agent_id, t, knowledge_id)
                    # "찾을 수 없습니다"인 경우만 다음 타입으로
                    if "찾을 수 없습니다" in result:
                        continue
                    return result
                except Exception:
                    continue
            return f"❌ ID/이름이 '{knowledge_id}'인 지식을 찾을 수 없습니다. (AUTO 조회)"

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
            # 직접 조회: 업로드 스킬(HTTP API) → 내장 스킬(MCP) 순서 (벡터 검색 100개 회피)
            target = None
            _skill_name = knowledge_id
            try:
                from core.skill_api_client import check_skill_exists_with_info as _check_skill, get_skill_file_content as _get_skill_file
                _info = _check_skill(_skill_name)
                if _info and _info.get("exists"):
                    _file = _get_skill_file(_skill_name, "SKILL.md")
                    target = {
                        "id": _skill_name, "name": _info.get("name", _skill_name),
                        "description": _info.get("description", ""),
                        "content": _file.get("content", ""),
                        "verified": True, "is_builtin": False,
                    }
            except Exception:
                pass
            if not target:
                from core.knowledge_retriever import _read_skill_document_mcp
                _content = await _read_skill_document_mcp(_skill_name, "SKILL.md")
                if _content:
                    target = {
                        "id": _skill_name, "name": _skill_name,
                        "description": "", "content": _content,
                        "verified": True, "is_builtin": True,
                    }
            if not target:
                return f"❌ ID/이름이 '{knowledge_id}'인 스킬을 찾을 수 없습니다."
            
            skill_name = target.get('name', target.get('id'))
            output_lines.append(f"🔑 ID/이름: {skill_name}")
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
            
            # 스킬의 모든 파일 내용 조회 (업로드된 스킬인 경우)
            try:
                from core.skill_api_client import get_skill_files, check_skill_exists, get_skill_file_content
                if check_skill_exists(skill_name):
                    skill_files = get_skill_files(skill_name)
                    if skill_files:
                        output_lines.append(f"\n📁 스킬 디렉토리 파일 ({len(skill_files)}개):")
                        
                        # 모든 텍스트 파일의 내용 조회
                        text_files_found = 0
                        for file_info in skill_files:
                            file_path = file_info.get("path", "")
                            file_size = file_info.get("size", 0)
                            
                            try:
                                # 파일 내용 조회 (텍스트 파일만)
                                file_content_info = get_skill_file_content(skill_name, file_path)
                                file_type = file_content_info.get("type", "")
                                file_content = file_content_info.get("content", "")
                                
                                if file_type == "text" and file_content:
                                    text_files_found += 1
                                    # 파일 확장자에 따라 코드블록 언어 결정
                                    file_ext = file_path.split(".")[-1].lower() if "." in file_path else ""
                                    lang_map = {
                                        "py": "python",
                                        "md": "markdown",
                                        "json": "json",
                                        "yaml": "yaml",
                                        "yml": "yaml",
                                        "txt": "text",
                                        "sh": "bash",
                                        "js": "javascript",
                                        "ts": "typescript",
                                        "html": "html",
                                        "css": "css",
                                    }
                                    lang = lang_map.get(file_ext, "text")
                                    
                                    output_lines.append(f"\n📄 {file_path} ({file_size} bytes):")
                                    output_lines.append(f"```{lang}")
                                    output_lines.append(file_content)
                                    output_lines.append("```")
                                else:
                                    # 바이너리 파일이거나 내용이 없는 경우
                                    output_lines.append(f"\n📄 {file_path} ({file_size} bytes, {file_type} file)")
                            except Exception as e:
                                # 파일 조회 실패 시 경로만 표시
                                log(f"   ⚠️ 파일 내용 조회 실패 ({file_path}): {e}")
                                output_lines.append(f"\n📄 {file_path} ({file_size} bytes, 조회 실패)")
                        
                        if text_files_found > 0:
                            output_lines.append(f"\n💡 총 {text_files_found}개의 텍스트 파일 내용을 확인했습니다.")
                            output_lines.append("💡 파일을 수정하려면 commit_skill 도구의 additional_files 파라미터에 파일 경로와 수정된 내용을 포함하세요.")
            except Exception as e:
                log(f"   ⚠️ 스킬 파일 조회 실패: {e}")
        
        else:
            return f"❌ 지원하지 않는 지식 타입: {knowledge_type}"
        
        output_lines.append("")
        output_lines.append("━" * 50)
        if knowledge_type == "SKILL":
            output_lines.append("🧠 스킬의 모든 파일 내용을 검토하여 피드백과 비교하세요:")
            output_lines.append("   - 피드백이 어떤 파일과 관련되어 있는가? (SKILL.md, scripts/, references/ 등)")
            output_lines.append("   - 어떤 파일을 수정해야 하는가?")
            output_lines.append("   - 새 파일을 추가해야 하는가?")
            output_lines.append("   - 병합/수정이 필요하면 commit_skill 도구의 additional_files에 파일 경로와 수정된 내용을 포함하세요.")
            output_lines.append("   - 피드백이 기존 스킬에 통합 가능하면 CREATE보다 UPDATE를 우선 고려하세요.")
        else:
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

def create_react_tools(agent_id: str, feedback_content: Optional[str] = None) -> List[StructuredTool]:
    """
    ReAct 에이전트용 도구 목록 생성
    
    Args:
        agent_id: 에이전트 ID (도구에 기본값으로 사용)
        feedback_content: 원본 피드백 내용 (commit_to_skill의 record_knowledge_history용, 선택)
    
    Returns:
        LangChain Tool 목록
    """
    
    # agent_id, feedback_content를 클로저로 캡처하는 래퍼 함수들 (완전 async)
    async def search_memory_wrapper(query: str, limit: int = 10) -> str:
        """메모리 검색 도구 (async)"""
        return await _search_memory_tool(agent_id, query, limit)
    
    async def search_dmn_rules_wrapper(search_text: str = "") -> str:
        """DMN 규칙 검색 도구 (async)"""
        return await _search_dmn_rules_tool(agent_id, search_text)
    
    async def search_skills_wrapper(search_text: str = "", top_k: int = 10) -> str:
        """Skills 검색 도구 (async)"""
        return await _search_skills_tool(agent_id, search_text, top_k)
    
    async def analyze_conflict_wrapper(new_knowledge_json: str, existing_knowledge_json: str, target_type: str) -> str:
        """충돌 분석 도구 (async) - JSON 문자열을 파싱하여 딕셔너리로 변환"""
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

            return await _analyze_conflict_tool(new_knowledge, existing_knowledge, target_type)
        except (json.JSONDecodeError, ValueError) as e:
            return f"❌ JSON 파싱 실패: {str(e)}\n입력된 new_knowledge_json (첫 500자): {str(new_knowledge_json)[:500]}...\n입력된 existing_knowledge_json (첫 500자): {str(existing_knowledge_json)[:500]}..."
        except Exception as e:
            return f"❌ 충돌 분석 실패: {str(e)}"
    
    async def get_knowledge_detail_wrapper(knowledge_type: str, knowledge_id: str = "") -> str:
        """기존 지식 상세 조회 도구 (async) - kwargs 형식 입력 처리"""
        import re
        import json
        
        actual_knowledge_type = knowledge_type
        actual_knowledge_id = knowledge_id
        
        # ReAct(text) 에이전트는 Action Input(JSON)을 문자열로 넘길 수 있어,
        # 이 경우 knowledge_type 파라미터에 JSON 문자열이 통째로 들어온다.
        if isinstance(knowledge_type, str):
            input_str = knowledge_type.strip()

            # 1) JSON 문자열로 들어온 경우 복구 ({"skill_id": "..."} / {"knowledge_id": "..."} / {"knowledge_type": "...", ...})
            if input_str.startswith("{") and input_str.endswith("}"):
                try:
                    parsed = json.loads(input_str)
                    if isinstance(parsed, dict):
                        # skill_id만 주는 실수를 흔히 함 → SKILL로 간주
                        if not actual_knowledge_id and parsed.get("skill_id"):
                            actual_knowledge_type = "SKILL"
                            actual_knowledge_id = str(parsed.get("skill_id"))
                        # knowledge_id만 준 경우 → AUTO로 조회
                        if not actual_knowledge_id and parsed.get("knowledge_id"):
                            actual_knowledge_type = parsed.get("knowledge_type") or "AUTO"
                            actual_knowledge_id = str(parsed.get("knowledge_id"))
                        # 정상 케이스
                        if parsed.get("knowledge_type"):
                            actual_knowledge_type = str(parsed.get("knowledge_type"))
                        if parsed.get("knowledge_id"):
                            actual_knowledge_id = str(parsed.get("knowledge_id"))
                except Exception:
                    pass

            # 2) kwargs 형식 문자열인 경우 복구
            if 'knowledge_type=' in input_str or 'knowledge_id=' in input_str or 'skill_id=' in input_str:
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

                # skill_id 추출 → SKILL로 간주
                sid_match = re.search(r'skill_id\s*=\s*["\']?([^"\'",\s]+)["\']?', input_str)
                if sid_match and not actual_knowledge_id:
                    actual_knowledge_type = "SKILL"
                    actual_knowledge_id = sid_match.group(1)
                    log(f"   추출된 skill_id → knowledge_id: {actual_knowledge_id}")
        
        # knowledge_id가 없으면 에러
        if not actual_knowledge_id:
            return f"❌ knowledge_id가 필요합니다. 입력값: knowledge_type={actual_knowledge_type}"

        # knowledge_type이 비정상/누락이면 AUTO로 복구
        if not actual_knowledge_type or (isinstance(actual_knowledge_type, str) and actual_knowledge_type.strip() == ""):
            actual_knowledge_type = "AUTO"
        actual_knowledge_type = str(actual_knowledge_type).upper().strip()
        if actual_knowledge_type not in ["MEMORY", "DMN_RULE", "SKILL", "AUTO"]:
            actual_knowledge_type = "AUTO"

        return await _get_knowledge_detail_tool(agent_id, actual_knowledge_type, actual_knowledge_id)
    
    async def commit_memory_wrapper(content: str, operation: str = "CREATE", memory_id: Optional[str] = None) -> str:
        """메모리 저장 도구 (async)"""
        return await _commit_memory_tool(agent_id, content, operation, memory_id)
    
    async def commit_dmn_rule_wrapper(dmn_artifact_json: str, operation: str = "CREATE", rule_id: Optional[str] = None, feedback_content: str = "", merge_mode: Optional[str] = "REPLACE") -> str:
        """DMN 규칙 저장 도구 (async) - JSON 문자열을 파싱하여 딕셔너리로 변환"""
        import json
        import re
        
        # 에이전트가 kwargs 형식으로 전달한 경우 파싱
        # 예: dmn_artifact_json='{"name": "..."}', operation="UPDATE", rule_id="...", merge_mode="EXTEND"
        actual_operation = operation
        actual_rule_id = rule_id
        actual_merge_mode = merge_mode
        actual_json = dmn_artifact_json  # 초기값 설정
        
        log(f"🔍 commit_dmn_rule_wrapper 시작: operation={operation}, rule_id={rule_id}, merge_mode={merge_mode}")
        log(f"   dmn_artifact_json 타입: {type(dmn_artifact_json).__name__}")
        
        # LangChain이 딕셔너리를 직접 전달할 수 있으므로 처리
        if isinstance(dmn_artifact_json, dict):
            # 이미 딕셔너리인 경우 그대로 사용하고 JSON 파싱 단계 건너뛰기
            log(f"ℹ️ dmn_artifact_json이 이미 딕셔너리로 전달됨: {list(dmn_artifact_json.keys())}")
            log(f"   딕셔너리 내용: {json.dumps(dmn_artifact_json, ensure_ascii=False)[:500]}")
            
            # ⚠️ 중요: 딕셔너리에서 operation, rule_id, merge_mode를 먼저 추출 (중첩 구조 처리 전에)
            # LangChain이 딕셔너리를 전달할 때, 다른 파라미터들이 기본값으로 설정될 수 있으므로
            # dmn_artifact_json 딕셔너리 내부에서 메타데이터를 추출해야 함
            if "operation" in dmn_artifact_json:
                extracted_op = dmn_artifact_json.get("operation")
                log(f"   🔍 operation 키 발견: {repr(extracted_op)} (타입: {type(extracted_op).__name__})")
                if extracted_op and str(extracted_op).strip():
                    actual_operation = str(extracted_op).strip().upper()  # 대문자로 정규화
                    log(f"   ✅ 딕셔너리에서 operation 추출: {actual_operation} (함수 파라미터: {operation})")
                else:
                    log(f"   ⚠️ operation 값이 비어있음: {repr(extracted_op)}")
            else:
                log(f"   ⚠️ operation 키가 딕셔너리에 없음")
            
            if "rule_id" in dmn_artifact_json:
                extracted_rid = dmn_artifact_json.get("rule_id")
                log(f"   🔍 rule_id 키 발견: {repr(extracted_rid)} (타입: {type(extracted_rid).__name__})")
                if extracted_rid and str(extracted_rid).strip():
                    actual_rule_id = str(extracted_rid).strip()
                    log(f"   ✅ 딕셔너리에서 rule_id 추출: {actual_rule_id} (함수 파라미터: {rule_id})")
                else:
                    log(f"   ⚠️ rule_id 값이 비어있음: {repr(extracted_rid)}")
            else:
                log(f"   ⚠️ rule_id 키가 딕셔너리에 없음")
            
            if "merge_mode" in dmn_artifact_json:
                extracted_mm = dmn_artifact_json.get("merge_mode")
                log(f"   🔍 merge_mode 키 발견: {repr(extracted_mm)} (타입: {type(extracted_mm).__name__})")
                if extracted_mm and str(extracted_mm).strip():
                    actual_merge_mode = str(extracted_mm).strip().upper()  # 대문자로 정규화
                    log(f"   ✅ 딕셔너리에서 merge_mode 추출: {actual_merge_mode} (함수 파라미터: {merge_mode})")
                else:
                    log(f"   ⚠️ merge_mode 값이 비어있음: {repr(extracted_mm)}")
            else:
                log(f"   ⚠️ merge_mode 키가 딕셔너리에 없음")
            
            log(f"   📊 추출 결과: actual_operation={actual_operation}, actual_rule_id={actual_rule_id}, actual_merge_mode={actual_merge_mode}")
            
            # 딕셔너리 안에 "dmn_artifact_json" 키가 있는지 확인 (중첩된 경우)
            if "dmn_artifact_json" in dmn_artifact_json:
                # 중첩된 구조: {"dmn_artifact_json": {...}, "operation": "CREATE"}
                nested_artifact = dmn_artifact_json.get("dmn_artifact_json")
                log(f"   중첩된 dmn_artifact_json 발견, 추출 중...")
                
                # 중첩된 dmn_artifact_json을 사용하되, 메타데이터(operation, rule_id, merge_mode)는 유지
                if isinstance(nested_artifact, dict):
                    # 중첩된 구조에 메타데이터를 추가하여 전달 (extract_nested_artifact에서 추출할 수 있도록)
                    actual_json = {
                        "dmn_artifact_json": nested_artifact,
                        "operation": actual_operation,  # 이미 추출된 값 사용
                        "rule_id": actual_rule_id,      # 이미 추출된 값 사용
                        "merge_mode": actual_merge_mode # 이미 추출된 값 사용
                    }
                    log(f"   중첩 구조 + 메타데이터로 actual_json 구성: operation={actual_operation}, rule_id={actual_rule_id}")
                elif isinstance(nested_artifact, str):
                    actual_json = nested_artifact  # 문자열이면 나중에 파싱
                else:
                    actual_json = dmn_artifact_json  # 폴백
            else:
                # 일반 딕셔너리인 경우 그대로 사용 (메타데이터가 이미 포함되어 있음)
                actual_json = dmn_artifact_json  # 딕셔너리로 유지하여 try 블록에서 처리
        elif isinstance(dmn_artifact_json, str):
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
                
                # merge_mode 추출
                mm_match = re.search(r'merge_mode\s*=\s*["\']?(\w+)["\']?', input_str)
                if mm_match:
                    actual_merge_mode = mm_match.group(1)
                    log(f"   추출된 merge_mode: {actual_merge_mode}")
                
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
            # 메타데이터 및 중첩 artifact를 추출하는 공통 헬퍼
            def _extract_meta_and_artifact_from_dict(obj: dict):
                nonlocal actual_operation, actual_rule_id, actual_merge_mode

                # ⚠️ 중요: 딕셔너리에서 operation, rule_id, merge_mode를 먼저 추출
                if "operation" in obj:
                    extracted_op = obj.get("operation")
                    if extracted_op:
                        actual_operation = extracted_op
                        log(f"   actual_json에서 operation 추출: {actual_operation}")

                if "rule_id" in obj:
                    extracted_rid = obj.get("rule_id")
                    if extracted_rid:
                        actual_rule_id = extracted_rid
                        log(f"   actual_json에서 rule_id 추출: {actual_rule_id}")

                if "merge_mode" in obj:
                    extracted_mm = obj.get("merge_mode")
                    if extracted_mm:
                        actual_merge_mode = extracted_mm
                        log(f"   actual_json에서 merge_mode 추출: {actual_merge_mode}")

                dmn_obj = obj

                # 재귀적으로 중첩 구조를 처리하는 함수
                def extract_nested_artifact(inner_obj, depth=0, max_depth=5):
                    """중첩된 구조에서 실제 artifact를 재귀적으로 추출"""
                    nonlocal actual_operation, actual_rule_id, actual_merge_mode

                    if depth > max_depth:
                        return inner_obj

                    if not isinstance(inner_obj, dict):
                        return inner_obj

                    # operation, rule_id, merge_mode 등 메타데이터 추출 (추가 안전장치)
                    if "operation" in inner_obj:
                        extracted_op = inner_obj.get("operation")
                        if extracted_op:
                            actual_operation = extracted_op
                            log(f"   extract_nested_artifact에서 operation 추출 (depth={depth}): {actual_operation}")
                    if "rule_id" in inner_obj:
                        extracted_rid = inner_obj.get("rule_id")
                        if extracted_rid:
                            actual_rule_id = extracted_rid
                            log(f"   extract_nested_artifact에서 rule_id 추출 (depth={depth}): {actual_rule_id}")
                    if "merge_mode" in inner_obj:
                        extracted_mm = inner_obj.get("merge_mode")
                        if extracted_mm:
                            actual_merge_mode = extracted_mm
                            log(f"   extract_nested_artifact에서 merge_mode 추출 (depth={depth}): {actual_merge_mode}")

                    # "dmn_artifact_json" 키가 있으면 재귀적으로 추출
                    if "dmn_artifact_json" in inner_obj:
                        nested = inner_obj["dmn_artifact_json"]
                        log(f"   중첩된 dmn_artifact_json 발견 (depth={depth}), 재귀 추출 중...")
                        return extract_nested_artifact(nested, depth + 1, max_depth)

                    # condition과 action이 직접 있는지 확인
                    if "condition" in inner_obj and "action" in inner_obj:
                        return inner_obj

                    # rules 배열이 있는지 확인
                    if "rules" in inner_obj and isinstance(inner_obj.get("rules"), list):
                        return inner_obj

                    # 그 외에는 그대로 반환
                    return inner_obj

                extracted = extract_nested_artifact(dmn_obj)
                log(f"   최종 추출된 dmn_artifact 키: {list(extracted.keys()) if isinstance(extracted, dict) else 'N/A'}")
                log(f"   최종 actual_operation={actual_operation}, actual_rule_id={actual_rule_id}, actual_merge_mode={actual_merge_mode}")
                return extracted

            # 입력 타입에 따라 처리
            if isinstance(actual_json, dict):
                dmn_artifact = _extract_meta_and_artifact_from_dict(actual_json)
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
                    # 문자열을 JSON으로 파싱한 뒤, 딕셔너리 처리 로직을 그대로 재사용
                    parsed = json.loads(actual_json)
                    if not isinstance(parsed, dict):
                        return f"❌ dmn_artifact_json 파싱 결과가 dict가 아닙니다. type={type(parsed).__name__}"
                    dmn_artifact = _extract_meta_and_artifact_from_dict(parsed)
                except json.JSONDecodeError as e:
                    # 파싱 실패 시 더 자세한 에러 정보
                    return f"❌ JSON 파싱 실패: {str(e)}\n입력된 dmn_artifact_json (첫 500자): {actual_json[:500]}...\n입력 타입: {type(actual_json).__name__}"
            else:
                return f"❌ 지원하지 않는 입력 타입: {type(actual_json).__name__}\n입력된 값: {str(actual_json)[:200]}..."
            
            # condition과 action을 찾는 함수 (재귀적으로 탐색)
            def find_condition_and_action(obj, depth=0, max_depth=5):
                """재귀적으로 condition과 action을 찾기"""
                if depth > max_depth or not isinstance(obj, dict):
                    return None, None
                
                # 최상위 레벨에서 직접 찾기
                condition = obj.get("condition")
                action = obj.get("action")
                if condition and action:
                    # 빈 문자열이 아닌지 확인
                    if isinstance(condition, str) and condition.strip() and isinstance(action, str) and action.strip():
                        return condition, action
                
                # rules 배열에서 찾기
                if "rules" in obj and isinstance(obj.get("rules"), list):
                    rules = obj["rules"]
                    if len(rules) > 0:
                        first_rule = rules[0]
                        if isinstance(first_rule, dict):
                            # condition/action 형식
                            rule_condition = first_rule.get("condition")
                            rule_action = first_rule.get("action")
                            if rule_condition and rule_action:
                                if isinstance(rule_condition, str) and rule_condition.strip() and isinstance(rule_action, str) and rule_action.strip():
                                    # 여러 규칙이 있으면 병합
                                    if len(rules) > 1:
                                        conditions = [r.get("condition", "") for r in rules if isinstance(r, dict) and r.get("condition")]
                                        actions = [r.get("action", "") for r in rules if isinstance(r, dict) and r.get("action")]
                                        if conditions and actions:
                                            merged_condition = " 또는 ".join([f"({c})" for c in conditions if c])
                                            merged_action = "; ".join([a for a in actions if a])
                                            return merged_condition, merged_action
                                    return rule_condition, rule_action
                            
                            # input/output 형식
                            rule_input = first_rule.get("input")
                            rule_output = first_rule.get("output")
                            if rule_input and rule_output:
                                if isinstance(rule_input, str) and rule_input.strip() and isinstance(rule_output, str) and rule_output.strip():
                                    if len(rules) > 1:
                                        inputs = [r.get("input", "") for r in rules if isinstance(r, dict) and r.get("input")]
                                        outputs = [r.get("output", "") for r in rules if isinstance(r, dict) and r.get("output")]
                                        if inputs and outputs:
                                            merged_condition = " 또는 ".join([f"({i})" for i in inputs if i])
                                            merged_action = "; ".join([o for o in outputs if o])
                                            return merged_condition, merged_action
                                    return rule_input, rule_output
                
                # 중첩된 구조에서 재귀적으로 찾기
                for key, value in obj.items():
                    if key in ["dmn_artifact_json", "artifact", "rule", "data"] and isinstance(value, dict):
                        nested_condition, nested_action = find_condition_and_action(value, depth + 1, max_depth)
                        if nested_condition and nested_action:
                            return nested_condition, nested_action
                
                return None, None
            
            # condition과 action 찾기 (재귀적으로 모든 구조 탐색)
            condition, action = find_condition_and_action(dmn_artifact)
            
            # 디버깅을 위한 상세 로그
            log(f"🔍 DMN artifact 검증: condition={repr(condition)}, action={repr(action)}")
            log(f"🔍 DMN artifact 전체: {json.dumps(dmn_artifact, ensure_ascii=False, indent=2)}")
            
            # condition과 action이 없거나 빈 문자열인지 확인
            if not condition or (isinstance(condition, str) and not condition.strip()):
                return f"❌ condition이 필요합니다 (비어있거나 None). 전달된 데이터: {json.dumps(dmn_artifact, ensure_ascii=False)[:500]}..."
            
            if not action or (isinstance(action, str) and not action.strip()):
                return f"❌ action이 필요합니다 (비어있거나 None). 전달된 데이터: {json.dumps(dmn_artifact, ensure_ascii=False)[:500]}..."
            
            # condition과 action을 찾았으므로, dmn_artifact를 완전히 정규화된 형태로 재구성
            # 중첩 구조를 제거하고 최상위에 condition, action, name만 있는 깔끔한 딕셔너리로 만듦
            # 이름이 비어 있으면 여기서는 채우지 않고, commit_to_dmn_rule 단계에서 안전한 기본값을 적용한다.
            normalized_dmn_artifact = {
                "name": (dmn_artifact.get("name") or "").strip() or None,
                "condition": condition,
                "action": action
            }
            
            log(f"✅ 최종 추출 완료: condition={condition[:50]}..., action={action[:50]}...")
            log(f"✅ 정규화된 dmn_artifact: {json.dumps(normalized_dmn_artifact, ensure_ascii=False)}")
            
            # 추출된 operation/rule_id 로깅 및 최종 검증
            log(f"📋 DMN 규칙 저장 호출: operation={actual_operation}, rule_id={actual_rule_id}, merge_mode={actual_merge_mode}")
            
            # ⚠️ 중요: rule_id가 있는데 operation이 CREATE이면 에러
            if actual_rule_id and actual_rule_id.strip() and actual_operation == "CREATE":
                log(f"⚠️ 경고: rule_id가 있는데 operation이 CREATE입니다. UPDATE로 변경합니다.")
                actual_operation = "UPDATE"
                log(f"   수정된 operation: {actual_operation}")
            
            # ⚠️ 중요: operation이 UPDATE인데 rule_id가 없으면 에러
            if actual_operation == "UPDATE" and (not actual_rule_id or not actual_rule_id.strip()):
                return f"❌ DMN 규칙 저장 실패: UPDATE 작업에는 rule_id가 필수입니다. rule_id를 제공해주세요. (현재: operation={actual_operation}, rule_id={actual_rule_id})"

            # merge_mode에 따라 도구가 안전하게 병합 처리
            # 정규화된 dmn_artifact를 전달 (중첩 구조 제거)
            return await _commit_dmn_rule_tool(agent_id, normalized_dmn_artifact, actual_operation, actual_rule_id, feedback_content, actual_merge_mode)
        except json.JSONDecodeError as e:
            return f"❌ JSON 파싱 실패: {str(e)}\n입력된 dmn_artifact_json: {actual_json[:200] if isinstance(actual_json, str) else str(actual_json)[:200]}..."
        except Exception as e:
            return f"❌ DMN 규칙 저장 실패: {str(e)}"
    
    async def commit_skill_wrapper(
        operation: str = "CREATE",
        skill_id: Optional[str] = None,
        merge_mode: str = "MERGE",
        relationship_analysis: Optional[str] = None,
        related_skill_ids: Optional[str] = None,
    ) -> str:
        """Skill 저장 도구 (async). 스킬 내용(SKILL.md, steps, additional_files)은 skill-creator가 생성. feedback_content는 자동 전달."""
        import json as _json
        actual_op = operation
        actual_sid = skill_id
        actual_mm = merge_mode or "MERGE"
        actual_ra = relationship_analysis
        actual_related = related_skill_ids
        # ReAct이 Action Input에 {"operation":"UPDATE","skill_id":"x",...} 전체를 넘기면, 첫 파라미터(operation)에 그대로 들어올 수 있음. 언랩.
        def _unwrap(obj: dict) -> None:
            nonlocal actual_op, actual_sid, actual_mm, actual_ra, actual_related
            if isinstance(obj, dict):
                if obj.get("operation") is not None:
                    actual_op = str(obj.get("operation", "CREATE")).strip().upper()
                if obj.get("skill_id") is not None:
                    actual_sid = obj.get("skill_id") or actual_sid
                if obj.get("merge_mode") is not None:
                    actual_mm = str(obj.get("merge_mode", "MERGE")).strip()
                if obj.get("relationship_analysis") is not None:
                    actual_ra = (obj.get("relationship_analysis") or "").strip() or None
                if obj.get("related_skill_ids") is not None:
                    val = obj.get("related_skill_ids")
                    # 리스트/문자열/기타 어떤 형태든 안전하게 쉼표 문자열로 정규화
                    if isinstance(val, list):
                        joined = ",".join(str(x).strip() for x in val if str(x).strip())
                        actual_related = joined or None
                    else:
                        s = "" if val is None else str(val)
                        actual_related = s.strip() or None

        if isinstance(operation, dict):
            _unwrap(operation)
            log(f"🔧 commit_to_skill: dict 언랩 → operation={actual_op}, skill_id={actual_sid}, merge_mode={actual_mm}")
        elif isinstance(operation, str) and operation.strip().startswith("{"):
            try:
                o = _json.loads(operation)
                if isinstance(o, dict):
                    _unwrap(o)
                    log(f"🔧 commit_to_skill: JSON 문자열 언랩 → operation={actual_op}, skill_id={actual_sid}, merge_mode={actual_mm}")
            except _json.JSONDecodeError:
                pass
        if isinstance(actual_op, str) and actual_op.upper() not in ("CREATE", "UPDATE", "DELETE"):
            s = str(actual_op).strip()
            if s.startswith("{"):
                try:
                    o = _json.loads(s)
                    if isinstance(o, dict):
                        _unwrap(o)
                        log(f"🔧 commit_to_skill: operation 필드 JSON 재파싱 → operation={actual_op}, skill_id={actual_sid}")
                except _json.JSONDecodeError:
                    pass
        try:
            log(f"📋 commit_to_skill: operation={actual_op}, skill_id={actual_sid}, merge_mode={actual_mm}")
            return await _commit_skill_tool(
                agent_id=agent_id,
                operation=actual_op,
                skill_id=actual_sid,
                merge_mode=actual_mm,
                feedback_content=feedback_content or "",
                relationship_analysis=actual_ra,
                related_skill_ids=actual_related,
            )
        except Exception as e:
            return f"❌ Skill 저장 실패: {str(e)}"

    async def attach_skills_to_agent_wrapper(skill_ids: str) -> str:
        """기존 스킬을 에이전트에 적재합니다. 스킬 생성/수정 없이 에이전트에 추가만 합니다."""
        return await _attach_skills_to_agent_tool(agent_id=agent_id, skill_ids=skill_ids)

    # 새로운 통합 도구 래퍼 함수들
    async def search_similar_knowledge_wrapper(content: str, knowledge_type: str = "ALL", threshold: float = 0.7) -> str:
        """유사 지식 검색 도구 (async). 초기 지식 셋팅 시 feedback_content가 있으면 목표+페르소나를 검색에 사용."""
        actual_content = content
        if feedback_content and str(feedback_content).strip():
            # 에이전트가 목표만 넣었을 수 있음: content가 feedback_content보다 짧거나, '페르소나'를 포함하지 않으면 전체 문맥 사용
            agent_content = (content or "").strip()
            full_context = str(feedback_content).strip()
            if len(agent_content) < len(full_context) or "페르소나" not in agent_content:
                actual_content = full_context
        return await _search_similar_knowledge_tool(agent_id, actual_content, knowledge_type, threshold)
    
    async def check_duplicate_wrapper(content: str, knowledge_type: str, candidate_id: Optional[str] = None) -> str:
        """중복 확인 도구 (async)"""
        return await _check_duplicate_tool(agent_id, content, knowledge_type, candidate_id)
    
    async def determine_operation_wrapper(content: str, knowledge_type: str = "") -> str:
        """작업 결정 도구 (async) - kwargs 형식 입력 처리"""
        import re
        
        actual_content = content
        actual_knowledge_type = knowledge_type
        
        # 에이전트가 kwargs 형식으로 전달한 경우 파싱
        # 예: content="some content", knowledge_type="DMN"
        # 또는 content='content=...knowledge_type='DMN''
        if isinstance(content, str):
            input_str = content.strip()
            
            # kwargs 형식인지 확인 (content= 또는 knowledge_type= 포함)
            if 'knowledge_type=' in input_str or (not knowledge_type and ('content=' in input_str or 'knowledge_type=' in input_str)):
                log(f"🔧 determine_operation: kwargs 형식 입력 감지, 파싱 시도...")
                log(f"   입력값: {input_str}")
                
                # content 추출
                # content='...' 또는 content="..." 형태
                content_match = re.search(r'content\s*=\s*["\']([^"\']*)["\']', input_str)
                if content_match:
                    actual_content = content_match.group(1)
                    log(f"   추출된 content: {actual_content[:100]}...")
                else:
                    # content=...knowledge_type= 형태에서 content 부분만 추출
                    content_end = input_str.find('knowledge_type=')
                    if content_end > 0:
                        content_part = input_str[:content_end].strip()
                        if content_part.startswith('content='):
                            actual_content = content_part[8:].strip().strip("'\"")
                            log(f"   추출된 content (후처리): {actual_content[:100]}...")
                
                # knowledge_type 추출
                type_match = re.search(r'knowledge_type\s*=\s*["\']?([^"\'",\s]+)["\']?', input_str)
                if type_match:
                    actual_knowledge_type = type_match.group(1)
                    log(f"   추출된 knowledge_type: {actual_knowledge_type}")
        
        # knowledge_type이 없으면 에러
        if not actual_knowledge_type:
            return f"❌ knowledge_type이 필요합니다. 입력값: content={actual_content[:100]}..."
        
        # content가 비어있으면 에러
        if not actual_content:
            return f"❌ content가 필요합니다. 입력값: knowledge_type={actual_knowledge_type}"

        return await _determine_operation_tool(agent_id, actual_content, actual_knowledge_type)
    
    tools = [
        StructuredTool.from_function(
            coroutine=search_memory_wrapper,
            name="search_memory",
            description="mem0에서 관련 메모리를 검색합니다. 피드백 내용과 유사한 기존 지식을 찾을 때 사용합니다.",
            args_schema=SearchMemoryInput
        ),
        StructuredTool.from_function(
            coroutine=search_dmn_rules_wrapper,
            name="search_dmn_rules",
            description="DMN 규칙을 검색합니다. 조건-결과 형태의 비즈니스 판단 규칙을 찾을 때 사용합니다.",
            args_schema=SearchDmnRulesInput
        ),
        StructuredTool.from_function(
            coroutine=search_skills_wrapper,
            name="search_skills",
            description="Skills를 검색합니다. 반복 가능한 절차나 작업 순서를 찾을 때 사용합니다.",
            args_schema=SearchSkillsInput
        ),
        # 새로운 통합 도구들 (의미적 유사도 기반)
        StructuredTool.from_function(
            coroutine=search_similar_knowledge_wrapper,
            name="search_similar_knowledge",
            description="""모든 저장소에서 의미적으로 유사한 기존 지식을 검색하고 관계를 분석합니다.
피드백을 저장하기 전에 반드시 이 도구를 먼저 사용하세요.
검색 결과에서 관계 유형(DUPLICATE, EXTENDS, REFINES, CONFLICTS 등)을 확인하고,
기존 지식과 새 피드백의 관계를 직접 분석하여 처리 방법을 결정하세요.""",
            args_schema=SearchSimilarKnowledgeInput
        ),
        StructuredTool.from_function(
            coroutine=check_duplicate_wrapper,
            name="check_duplicate",
            description="""특정 지식이 기존 지식과 중복인지 상세 확인합니다.
search_similar_knowledge로 유사한 지식을 찾은 후, 정확한 중복 여부를 확인할 때 사용합니다.""",
            args_schema=CheckDuplicateInput
        ),
        StructuredTool.from_function(
            coroutine=determine_operation_wrapper,
            name="determine_operation",
            description="""새 지식과 기존 지식의 관계를 분석하여 정보를 제공합니다.
관계 유형(DUPLICATE, EXTENDS, REFINES, CONFLICTS 등)과 상세 분석 결과를 반환합니다.
⚠️ 이 도구는 작업을 결정하지 않습니다. 제공된 정보를 바탕으로 직접 판단하세요.""",
            args_schema=DetermineOperationInput
        ),
        StructuredTool.from_function(
            coroutine=get_knowledge_detail_wrapper,
            name="get_knowledge_detail",
            description="""기존 지식의 전체 상세 내용을 조회합니다.
기존 지식과 새 피드백을 직접 비교하여 병합/수정 방법을 결정할 때 사용합니다.
DMN 규칙의 경우 전체 XML을, SKILL의 경우 전체 steps를 반환합니다.
병합이 필요하면 이 도구로 기존 내용을 조회한 후 직접 합쳐서 저장하세요.""",
            args_schema=GetKnowledgeDetailInput
        ),
        StructuredTool.from_function(
            coroutine=commit_memory_wrapper,
            name="commit_to_memory",
            description="mem0에 메모리를 저장/수정/삭제합니다. 지침, 선호도, 맥락 정보를 저장할 때 사용합니다. ⚠️ 메모리 내용은 항상 입력 피드백과 동일한 언어로 작성하세요 (예: 피드백이 한국어이면 메모리도 한국어로). 번역하거나 임의로 영어로 바꾸지 마세요.",
            args_schema=CommitMemoryInput
        ),
        StructuredTool.from_function(
            coroutine=commit_dmn_rule_wrapper,
            name="commit_to_dmn_rule",
            description="""DMN 규칙을 저장/수정/삭제합니다.

⚠️ 중요: 유사한 기존 규칙이 있으면 반드시 operation="UPDATE"와 rule_id를 함께 전달하세요!
- CREATE (기본값): 새 규칙 생성. 유사 규칙이 없을 때만 사용
- UPDATE: 기존 규칙 수정. 반드시 rule_id 필수!
- DELETE: 기존 규칙 삭제. 반드시 rule_id 필수!

merge_mode 파라미터 (UPDATE 시 중요):
- REPLACE (기본값): 완전 대체. 기존 구조 변경 가능. 에이전트가 전달한 내용이 최종 완성본.
- EXTEND: 기존 규칙 보존 + 새 규칙 추가. 도구가 자동으로 기존 XML 조회 및 병합.
- REFINE: 기존 규칙 참조 후 일부 수정 (현재는 REPLACE와 동일하게 처리).

관계 유형 → merge_mode 매핑:
- EXTENDS 관계 → merge_mode="EXTEND" (권장!)
- REFINES 관계 → merge_mode="REFINE"
- SUPERSEDES 관계 → merge_mode="REPLACE"

예시 (UPDATE + EXTEND): dmn_artifact_json='{"name": "규칙명", "condition": "조건", "action": "결과"}', operation="UPDATE", rule_id="기존_규칙_ID", merge_mode="EXTEND"
예시 (UPDATE + REPLACE): dmn_artifact_json='{"name": "규칙명", "condition": "조건", "action": "결과"}', operation="UPDATE", rule_id="기존_규칙_ID", merge_mode="REPLACE"
예시 (CREATE): dmn_artifact_json='{"name": "규칙명", "condition": "조건", "action": "결과"}'""",
            args_schema=CommitDmnRuleInput
        ),
        StructuredTool.from_function(
            coroutine=commit_skill_wrapper,
            name="commit_to_skill",
            description="Skill을 저장/수정/삭제합니다. **ReAct은 저장소(SKILL)·기존과의 관계(operation, skill_id)만 판단합니다.** 스킬 내용(SKILL.md, steps, additional_files)은 skill-creator가 생성. **목표/결과(예: 의사결정 기여)는 스킬 절차가 아님**—구체적 작업 절차·산출물이 기존 스킬로 부족할 때만 CREATE. 기존 스킬을 참조하는 새 스킬은 operation=CREATE, related_skill_ids=기존스킬이름(쉼표 구분). 동일 범위·동일 절차 수정 시에만 operation=UPDATE, skill_id=기존스킬이름. DELETE 시 skill_id 필수. search_similar_knowledge 결과는 relationship_analysis에 전달. 관련 스킬은 related_skill_ids에 전달.",
            args_schema=CommitSkillInput
        ),
        StructuredTool.from_function(
            coroutine=attach_skills_to_agent_wrapper,
            name="attach_skills_to_agent",
            description="""기존 스킬을 에이전트에 적재만 합니다. **스킬 생성/수정 없이** 에이전트에 추가합니다.
**사용 시점:** search_similar_knowledge에서 유사/연관 스킬을 찾았고, 기존 스킬이 **작업 절차·산출물**(데이터 수집, 보고서, 시각화 등)을 이미 커버할 때. 목표에 "의사결정 기여" 등이 있어도, 그 절차를 기존 스킬이 담당하면 attach만 하세요.
- 단일 스킬로 충분: skill_ids="skill-a"
- 여러 스킬 조합 필요: skill_ids="skill-a, skill-b, skill-c"
**주의:** 새 스킬 생성(commit_to_skill CREATE)이 아닌 기존 스킬 재사용입니다. 에이전트는 멀티 스킬 지원.""",
            args_schema=AttachSkillsToAgentInput
        ),
    ]
    
    return tools

