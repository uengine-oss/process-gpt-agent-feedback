"""
충돌 분석 모듈
새로운 피드백 기반 지식과 기존 지식을 비교하여 충돌 여부를 분석하고
적절한 CRUD 작업(Create/Update/Delete)을 결정
"""

import json
from typing import Dict, List, Optional
from llm_factory import create_llm
from utils.logger import log, handle_error


def clean_json_response(content: str) -> str:
    """LLM 응답에서 백틱과 json 키워드 제거"""
    content = content.replace("```json", "").replace("```", "")
    return content.strip()


async def analyze_knowledge_conflict(
    new_knowledge: Dict,
    existing_knowledge: Dict,
    target_type: str  # "MEMORY" | "DMN_RULE" | "SKILL"
) -> Dict:
    """
    새로운 지식과 기존 지식 간의 충돌을 분석하고 적절한 작업을 결정
    
    Args:
        new_knowledge: {
            "content": "..." (MEMORY용),
            "dmn": {...} (DMN_RULE용),
            "skill": {...} (SKILL용)
        }
        existing_knowledge: {
            "memories": [...],
            "dmn_rules": [...],
            "skills": [...]
        }
        target_type: 저장 대상 타입 ("MEMORY" | "DMN_RULE" | "SKILL")
    
    Returns:
        {
            "operation": "CREATE" | "UPDATE" | "DELETE" | "IGNORE",
            "conflict_level": "NONE" | "LOW" | "MEDIUM" | "HIGH",
            "conflict_reason": "...",
            "matched_item": {...} (UPDATE/DELETE 시 기존 항목 정보),
            "action_description": "작업 설명"
        }
    """
    
    llm = create_llm(model="gpt-4o", streaming=False, temperature=0)
    
    # 타겟 타입에 맞는 새 지식과 기존 지식 추출
    if target_type == "MEMORY":
        new_content = new_knowledge.get("content", "")
        existing_items = existing_knowledge.get("memories", [])
        existing_knowledge_text = _format_memories_for_analysis(existing_items)
        
    elif target_type == "DMN_RULE":
        new_dmn = new_knowledge.get("dmn", {})
        new_content = f"규칙명: {new_dmn.get('name', '')}, 조건: {new_dmn.get('condition', '')}, 결과: {new_dmn.get('action', '')}"
        existing_items = existing_knowledge.get("dmn_rules", [])
        existing_knowledge_text = _format_dmn_rules_for_analysis(existing_items)
        
    elif target_type == "SKILL":
        new_skill = new_knowledge.get("skill", {})
        new_content = f"스킬명: {new_skill.get('name', '')}, 단계: {new_skill.get('steps', [])}"
        existing_items = existing_knowledge.get("skills", [])
        existing_knowledge_text = _format_skills_for_analysis(existing_items)
        
    else:
        # 알 수 없는 타입
        return {
            "operation": "CREATE",
            "conflict_level": "NONE",
            "conflict_reason": "알 수 없는 타입",
            "matched_item": None,
            "action_description": "새 항목으로 생성"
        }
    
    # 기존 지식이 없으면 CREATE
    if not existing_items:
        log(f"📝 기존 {target_type} 지식이 없어 CREATE 작업 결정")
        return {
            "operation": "CREATE",
            "conflict_level": "NONE",
            "conflict_reason": "기존 지식 없음",
            "matched_item": None,
            "action_description": "새 항목으로 생성"
        }
    
    prompt = f"""
다음 새로운 지식과 기존 지식들을 비교하여 충돌 여부를 분석하고 적절한 작업을 결정해주세요.

**새로운 지식 ({target_type}):**
{new_content}

**기존 지식 목록 ({target_type}):**
{existing_knowledge_text}

**분석 기준:**

1. **충돌 판단 기준:**
   - **높은 충돌 (HIGH)**: 새로운 지식이 기존 지식과 정반대되거나 모순되는 경우
     예: 기존 "항상 X해야 함" vs 새로운 "X하지 않아야 함"
   - **중간 충돌 (MEDIUM)**: 새로운 지식이 기존 지식과 부분적으로 겹치지만 수정이 필요한 경우
     예: 기존 "주문 금액 >= 100만원" vs 새로운 "주문 금액 >= 150만원"
   - **낮은 충돌 (LOW)**: 새로운 지식이 기존 지식과 약간 겹치지만 보완 가능한 경우
     예: 기존 "X 방법 사용" vs 새로운 "X 방법 개선"
   - **충돌 없음 (NONE)**: 새로운 지식이 기존 지식과 관련이 없거나 독립적인 경우

2. **작업 결정 규칙:**
   - **CREATE**: 충돌이 없거나 기존 지식과 완전히 독립적인 경우
     - matched_item은 null
   - **UPDATE**: 기존 지식과 중간 이상 충돌이 있고, 새로운 지식이 기존 것을 대체/개선하는 경우
     - matched_item에 업데이트할 기존 항목의 정확한 ID와 내용을 포함 (위에 표시된 ID를 그대로 사용)
   - **DELETE**: 새로운 지식이 기존 지식이 잘못되었다고 명시적으로 지적하는 경우
     - matched_item에 삭제할 기존 항목의 정확한 ID를 포함 (위에 표시된 ID를 그대로 사용)
   - **IGNORE**: 새로운 지식이 기존 지식보다 가치가 낮거나 중복되는 경우
     - matched_item은 null

3. **우선순위:**
   - 새로운 피드백 기반 지식이 더 최신이고 정확하다고 가정
   - 충돌이 있으면 새로운 지식을 우선

**응답 형식:**
- 추가 설명 없이 오직 아래 JSON 구조로만 응답하세요
- 마크다운 코드블록(```)이나 기타 텍스트는 포함하지 마세요
- JSON 객체만 출력하세요

{{
  "operation": "CREATE | UPDATE | DELETE | IGNORE",
  "conflict_level": "NONE | LOW | MEDIUM | HIGH",
  "conflict_reason": "충돌 분석 이유 (한국어로 간단히 설명)",
  "matched_item": {{
    "id": "기존 항목의 ID (UPDATE/DELETE인 경우 필수)",
    "content": "기존 항목의 내용 요약",
    "similarity_score": 0.0-1.0
  }} (UPDATE/DELETE인 경우에만, 아니면 null),
  "action_description": "수행할 작업에 대한 설명 (한국어)"
}}
"""
    
    try:
        response = await llm.ainvoke(prompt)
        cleaned_content = clean_json_response(response.content)
        
        log(f"🔍 충돌 분석 LLM 응답: {cleaned_content[:500]}...")
        
        parsed_result = json.loads(cleaned_content)
        
        operation = parsed_result.get("operation", "CREATE")
        conflict_level = parsed_result.get("conflict_level", "NONE")
        matched_item = parsed_result.get("matched_item")
        
        log(f"📊 충돌 분석 결과: operation={operation}, conflict_level={conflict_level}")
        
        return {
            "operation": operation,
            "conflict_level": conflict_level,
            "conflict_reason": parsed_result.get("conflict_reason", ""),
            "matched_item": matched_item if matched_item else None,
            "action_description": parsed_result.get("action_description", "")
        }
        
    except json.JSONDecodeError as e:
        log(f"❌ 충돌 분석 JSON 파싱 실패: {e}")
        handle_error("충돌분석 JSON 파싱", f"응답 파싱 실패: {e}")
        # 기본값: CREATE
        return {
            "operation": "CREATE",
            "conflict_level": "NONE",
            "conflict_reason": "JSON 파싱 실패로 기본값 사용",
            "matched_item": None,
            "action_description": "새 항목으로 생성"
        }
    except Exception as e:
        handle_error("충돌분석", e)
        # 기본값: CREATE
        return {
            "operation": "CREATE",
            "conflict_level": "NONE",
            "conflict_reason": f"에러 발생: {str(e)}",
            "matched_item": None,
            "action_description": "새 항목으로 생성"
        }


def _format_memories_for_analysis(memories: List[Dict]) -> str:
    """메모리 목록을 분석용 텍스트로 포맷팅"""
    if not memories:
        return "기존 메모리 없음"
    
    formatted = []
    for idx, mem in enumerate(memories, start=1):
        memory_text = mem.get("memory", "")
        score = mem.get("score", 0)
        metadata = mem.get("metadata", {})
        mem_id = mem.get("id", f"memory_{idx}")
        formatted.append(f"[기존 메모리 {idx}] ID: {mem_id}, 관련도: {score:.2f}\n내용: {memory_text}")
    
    return "\n\n".join(formatted)


def _format_dmn_rules_for_analysis(dmn_rules: List[Dict]) -> str:
    """DMN 규칙 목록을 분석용 텍스트로 포맷팅"""
    if not dmn_rules:
        return "기존 DMN 규칙 없음"
    
    formatted = []
    for idx, rule in enumerate(dmn_rules, start=1):
        rule_id = rule.get("id", "")
        rule_name = rule.get("name", "")
        bpmn = rule.get("bpmn", "")
        # DMN XML에서 조건과 결과 추출 시도 (간단한 요약)
        formatted.append(f"[기존 DMN 규칙 {idx}] ID: {rule_id}, 이름: {rule_name}\nXML 내용: {bpmn[:500]}...")
    
    return "\n\n".join(formatted)


def _format_skills_for_analysis(skills: List[Dict]) -> str:
    """스킬 목록을 분석용 텍스트로 포맷팅"""
    if not skills:
        return "기존 스킬 없음"
    
    formatted = []
    for idx, skill in enumerate(skills, start=1):
        skill_id = skill.get("id", "")
        skill_name = skill.get("name", "")
        steps = skill.get("steps", [])
        formatted.append(f"[기존 스킬 {idx}] ID: {skill_id}, 이름: {skill_name}\n단계: {steps}")
    
    return "\n\n".join(formatted)

