"""
학습 커밋 라우터
route_learning 결과를 받아 기존 지식과 충돌을 분석한 후 적절한 CRUD 작업 수행
"""

from typing import Dict
from utils.logger import log, handle_error
from core.learning_committers import commit_to_memory, commit_to_dmn_rule, commit_to_skill
from core.knowledge_retriever import retrieve_all_existing_knowledge
from core.conflict_analyzer import analyze_knowledge_conflict


async def commit_learning(agent_id: str, routed_learning: Dict, original_content: str = ""):
    """
    route_learning 결과를 받아 기존 지식과 충돌을 분석한 후 적절한 CRUD 작업 수행
    
    Args:
        agent_id: 에이전트 ID
        routed_learning: {
            "target": "MEMORY | DMN_RULE | SKILL | MIXED",
            "artifacts": {
                "memory": "...",  # optional
                "dmn": {...},     # optional
                "skill": {...}    # optional
            },
            "reasoning": "..."
        }
        original_content: 원본 피드백 내용 (DMN 생성 시 더 정확한 XML 생성을 위해)
    
    Raises:
        Exception: 커밋 실패 시
    """
    try:
        target = routed_learning.get("target", "MEMORY")
        artifacts = routed_learning.get("artifacts", {})
        
        log(f"💾 학습 커밋 시작: 에이전트 {agent_id}, 타겟={target}")
        
        # 기존 지식 조회 (충돌 분석을 위해)
        existing_knowledge = await retrieve_all_existing_knowledge(agent_id, original_content)
        
        if target == "MEMORY":
            await _handle_memory_commit(agent_id, artifacts, existing_knowledge, original_content)
                
        elif target == "DMN_RULE":
            await _handle_dmn_commit(agent_id, artifacts, existing_knowledge, original_content)
                
        elif target == "SKILL":
            await _handle_skill_commit(agent_id, artifacts, existing_knowledge, original_content)
                
        elif target == "MIXED":
            await _handle_mixed_commit(agent_id, artifacts, existing_knowledge, original_content)
                
        else:
            log(f"⚠️ 알 수 없는 타겟: {target}, 기본값 MEMORY로 처리")
            await _handle_memory_commit(agent_id, artifacts, existing_knowledge, original_content)
        
        log(f"✅ 학습 커밋 완료: 에이전트 {agent_id}, 타겟={target}")
        
    except Exception as e:
        handle_error("학습커밋", e)
        raise


async def _handle_memory_commit(agent_id: str, artifacts: Dict, existing_knowledge: Dict, original_content: str):
    """MEMORY 타겟 처리 (충돌 분석 후 CRUD 작업)"""
    memory_content = artifacts.get("memory", "")
    if not memory_content:
        log(f"⚠️ MEMORY 타겟인데 content가 없음, artifacts: {artifacts}")
        return
    
    # 충돌 분석
    new_knowledge = {"content": memory_content}
    conflict_result = await analyze_knowledge_conflict(new_knowledge, existing_knowledge, "MEMORY")
    
    operation = conflict_result.get("operation", "CREATE")
    matched_item = conflict_result.get("matched_item")
    
    log(f"🔍 MEMORY 충돌 분석 결과: operation={operation}, conflict_level={conflict_result.get('conflict_level')}")
    
    # CRUD 작업 수행
    memory_id = None
    if matched_item and isinstance(matched_item, dict):
        memory_id = matched_item.get("id")
    
    if operation == "IGNORE":
        log(f"⏭️ MEMORY 무시: {conflict_result.get('action_description')}")
        return
    
    await commit_to_memory(
        agent_id=agent_id,
        content=memory_content,
        source_type="guideline",
        operation=operation,
        memory_id=memory_id
    )


async def _handle_dmn_commit(agent_id: str, artifacts: Dict, existing_knowledge: Dict, original_content: str):
    """DMN_RULE 타겟 처리 (충돌 분석 후 CRUD 작업)"""
    dmn_artifact = artifacts.get("dmn", {})
    if not dmn_artifact:
        log(f"⚠️ DMN_RULE 타겟인데 dmn artifact가 없음, artifacts: {artifacts}")
        return
    
    # 충돌 분석
    new_knowledge = {"dmn": dmn_artifact}
    conflict_result = await analyze_knowledge_conflict(new_knowledge, existing_knowledge, "DMN_RULE")
    
    operation = conflict_result.get("operation", "CREATE")
    matched_item = conflict_result.get("matched_item")
    
    log(f"🔍 DMN_RULE 충돌 분석 결과: operation={operation}, conflict_level={conflict_result.get('conflict_level')}")
    
    # CRUD 작업 수행
    rule_id = None
    if matched_item and isinstance(matched_item, dict):
        rule_id = matched_item.get("id")
    
    if operation == "IGNORE":
        log(f"⏭️ DMN_RULE 무시: {conflict_result.get('action_description')}")
        return
    
    await commit_to_dmn_rule(
        agent_id=agent_id,
        dmn_artifact=dmn_artifact,
        feedback_content=original_content,
        operation=operation,
        rule_id=rule_id
    )


async def _handle_skill_commit(agent_id: str, artifacts: Dict, existing_knowledge: Dict, original_content: str = ""):
    """SKILL 타겟 처리 (충돌 분석 후 CRUD 작업)"""
    skill_artifact = artifacts.get("skill", {})
    if not skill_artifact:
        log(f"⚠️ SKILL 타겟인데 skill artifact가 없음, artifacts: {artifacts}")
        return
    
    # 충돌 분석
    new_knowledge = {"skill": skill_artifact}
    conflict_result = await analyze_knowledge_conflict(new_knowledge, existing_knowledge, "SKILL")
    
    operation = conflict_result.get("operation", "CREATE")
    matched_item = conflict_result.get("matched_item")
    
    log(f"🔍 SKILL 충돌 분석 결과: operation={operation}, conflict_level={conflict_result.get('conflict_level')}")
    
    # CRUD 작업 수행
    skill_id = None
    if matched_item and isinstance(matched_item, dict):
        skill_id = matched_item.get("id")
    
    if operation == "IGNORE":
        log(f"⏭️ SKILL 무시: {conflict_result.get('action_description')}")
        return
    
    await commit_to_skill(
        agent_id=agent_id,
        skill_artifact=skill_artifact,
        operation=operation,
        skill_id=skill_id,
        feedback_content=original_content
    )


async def _handle_mixed_commit(agent_id: str, artifacts: Dict, existing_knowledge: Dict, original_content: str):
    """MIXED 타겟 처리 (각각 충돌 분석 후 CRUD 작업)"""
    log(f"🔀 MIXED 타입 분해 처리 시작")
    
    # DMN Rule이 있으면 우선 처리
    dmn_artifact = artifacts.get("dmn")
    if dmn_artifact:
        await _handle_dmn_commit(agent_id, {"dmn": dmn_artifact}, existing_knowledge, original_content)
    
    # Skill이 있으면 처리
    skill_artifact = artifacts.get("skill")
    if skill_artifact:
        await _handle_skill_commit(agent_id, {"skill": skill_artifact}, existing_knowledge, original_content)
    
    # MEMORY는 DMN/Skill이 없는 경우에만 저장
    # (우선순위: DMN_RULE > SKILL > MEMORY)
    memory_content = artifacts.get("memory")
    if memory_content and not dmn_artifact and not skill_artifact:
        await _handle_memory_commit(agent_id, {"memory": memory_content}, existing_knowledge, original_content)
    elif memory_content:
        log(f"📌 MEMORY는 DMN/Skill로 승격되어 mem0에 저장하지 않음 (우선순위 규칙)")
