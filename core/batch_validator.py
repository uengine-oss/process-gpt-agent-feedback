"""
배치 작업 검증 모듈
데이터 일관성, 의존성 체크, 안전장치 등 검증 기능
"""

from typing import Dict, List, Optional
from utils.logger import log, handle_error
from core.database import get_db_client
from core.knowledge_retriever import (
    get_memories_by_agent,
    retrieve_existing_dmn_rules,
    retrieve_existing_skills
)


async def validate_batch_plan(agent_id: str, plan: Dict) -> Dict:
    """
    배치 작업 계획 검증
    
    Args:
        agent_id: 에이전트 ID
        plan: generate_deduplication_plan()의 결과
    
    Returns:
        {
            "valid": bool,
            "warnings": List[str],
            "errors": List[str],
            "suggestions": List[str]
        }
    """
    try:
        log(f"🔍 배치 작업 계획 검증 시작: agent_id={agent_id}")
        
        warnings = []
        errors = []
        suggestions = []
        
        actions = plan.get("actions", [])
        summary = plan.get("summary", {})
        
        # 1. 삭제/이동 항목 수 검증
        to_delete = summary.get("to_delete", 0)
        to_move = len([a for a in actions if a.get("operation") == "MOVE"])
        
        if to_delete > 100:
            warnings.append(f"삭제 항목이 많습니다 ({to_delete}개). DRY_RUN으로 먼저 확인하세요.")
        
        if to_delete + to_move > 200:
            errors.append(f"삭제/이동 항목이 너무 많습니다 ({to_delete + to_move}개). 최대 200개까지 허용됩니다.")
        
        # 2. 의존성 체크
        dependency_issues = await check_dependencies(agent_id, actions)
        if dependency_issues:
            warnings.extend(dependency_issues)
        
        # 3. 데이터 일관성 검증
        consistency_issues = await check_data_consistency(agent_id, actions)
        if consistency_issues:
            warnings.extend(consistency_issues)
        
        # 4. 안전장치 검증
        safety_issues = await check_safety_limits(agent_id, actions, summary)
        if safety_issues:
            errors.extend(safety_issues)
        
        valid = len(errors) == 0
        
        log(f"✅ 배치 작업 계획 검증 완료: valid={valid}, 경고={len(warnings)}, 에러={len(errors)}")
        
        return {
            "valid": valid,
            "warnings": warnings,
            "errors": errors,
            "suggestions": suggestions
        }
        
    except Exception as e:
        handle_error("배치계획검증", e)
        return {
            "valid": False,
            "warnings": [],
            "errors": [f"검증 중 에러 발생: {e}"],
            "suggestions": []
        }


async def check_dependencies(agent_id: str, actions: List[Dict]) -> List[str]:
    """
    의존성 체크: 삭제/이동하려는 지식이 다른 지식에서 참조되는지 확인
    
    Args:
        agent_id: 에이전트 ID
        actions: 실행할 작업 목록
    
    Returns:
        의존성 경고 목록
    """
    warnings = []
    
    try:
        # 삭제/이동 대상 항목 수집
        items_to_delete = []
        items_to_move = []
        
        for action in actions:
            operation = action.get("operation")
            if operation == "DELETE":
                items_to_delete.append({
                    "storage": action.get("storage"),
                    "id": action.get("id")
                })
            elif operation == "MOVE":
                items_to_move.append({
                    "storage": action.get("from_storage"),
                    "id": action.get("id")
                })
        
        if not items_to_delete and not items_to_move:
            return warnings
        
        # 모든 지식 조회
        knowledge = await collect_agent_knowledge(agent_id)
        memories = knowledge.get("memories", [])
        dmn_rules = knowledge.get("dmn_rules", [])
        skills = knowledge.get("skills", [])
        
        # 간단한 의존성 체크 (내용 기반 유사도)
        # 실제로는 더 정교한 참조 관계 분석이 필요할 수 있음
        
        for item in items_to_delete + items_to_move:
            storage = item.get("storage")
            item_id = item.get("id")
            
            # SKILL 삭제 시 다른 지식에서 참조되는지 확인
            if storage == "SKILL":
                skill_name = item_id
                # SKILL 이름이 다른 지식의 내용에 포함되는지 확인
                for memory in memories:
                    memory_content = memory.get("memory") or memory.get("content", "")
                    if skill_name.lower() in memory_content.lower():
                        warnings.append(f"SKILL '{skill_name}'이 MEMORY에서 언급되고 있습니다. 삭제 전 확인하세요.")
                
                for rule in dmn_rules:
                    rule_name = rule.get("name", "")
                    rule_bpmn = rule.get("bpmn", "")
                    if skill_name.lower() in rule_name.lower() or skill_name.lower() in rule_bpmn.lower():
                        warnings.append(f"SKILL '{skill_name}'이 DMN_RULE '{rule_name}'에서 언급되고 있습니다. 삭제 전 확인하세요.")
            
            # DMN_RULE 삭제 시 다른 지식에서 참조되는지 확인
            elif storage == "DMN_RULE":
                rule_id = item_id
                rule_item = next((r for r in dmn_rules if r.get("id") == rule_id), None)
                if rule_item:
                    rule_name = rule_item.get("name", "")
                    # 규칙 이름이 다른 지식에서 언급되는지 확인
                    for memory in memories:
                        memory_content = memory.get("memory") or memory.get("content", "")
                        if rule_name.lower() in memory_content.lower():
                            warnings.append(f"DMN_RULE '{rule_name}'이 MEMORY에서 언급되고 있습니다. 삭제 전 확인하세요.")
        
        return warnings
        
    except Exception as e:
        log(f"⚠️ 의존성 체크 중 에러 (계속 진행): {e}")
        return warnings


async def check_data_consistency(agent_id: str, actions: List[Dict]) -> List[str]:
    """
    데이터 일관성 검증
    
    Args:
        agent_id: 에이전트 ID
        actions: 실행할 작업 목록
    
    Returns:
        일관성 경고 목록
    """
    warnings = []
    
    try:
        # 동일한 항목에 대한 중복 작업 체크
        item_operations = {}
        
        for action in actions:
            item_id = action.get("id")
            operation = action.get("operation")
            storage = action.get("storage")
            
            if item_id:
                key = f"{storage}:{item_id}"
                if key in item_operations:
                    warnings.append(f"항목 {key}에 대해 여러 작업이 계획되어 있습니다: {item_operations[key]}, {operation}")
                else:
                    item_operations[key] = operation
        
        # MOVE 작업의 일관성 체크
        for action in actions:
            if action.get("operation") == "MOVE":
                from_storage = action.get("from_storage")
                to_storage = action.get("to_storage")
                
                if from_storage == to_storage:
                    warnings.append(f"MOVE 작업에서 원본과 대상 저장소가 동일합니다: {from_storage}")
        
        return warnings
        
    except Exception as e:
        log(f"⚠️ 데이터 일관성 검증 중 에러 (계속 진행): {e}")
        return warnings


async def check_safety_limits(agent_id: str, actions: List[Dict], summary: Dict) -> List[str]:
    """
    안전장치 검증 (임계값 체크)
    
    Args:
        agent_id: 에이전트 ID
        actions: 실행할 작업 목록
        summary: 계획 요약
    
    Returns:
        안전장치 에러 목록
    """
    errors = []
    
    try:
        # 삭제/이동 항목 수 임계값
        to_delete = summary.get("to_delete", 0)
        to_move = len([a for a in actions if a.get("operation") == "MOVE"])
        total_changes = to_delete + to_move
        
        # 전체 지식 대비 변경 비율 계산
        knowledge = await collect_agent_knowledge(agent_id)
        total_knowledge = (
            len(knowledge.get("memories", [])) +
            len(knowledge.get("dmn_rules", [])) +
            len(knowledge.get("skills", []))
        )
        
        if total_knowledge > 0:
            change_ratio = total_changes / total_knowledge
            
            # 50% 이상 변경 시 에러
            if change_ratio > 0.5:
                errors.append(
                    f"변경 비율이 너무 높습니다 ({change_ratio:.1%}). "
                    f"전체 지식의 50% 이상을 변경하려고 합니다. "
                    f"DRY_RUN으로 먼저 확인하세요."
                )
            
            # 30% 이상 변경 시 경고 (하지만 에러는 아님)
            elif change_ratio > 0.3:
                log(f"⚠️ 변경 비율이 높습니다: {change_ratio:.1%}")
        
        # 절대값 임계값
        MAX_DELETE_LIMIT = 200
        if to_delete > MAX_DELETE_LIMIT:
            errors.append(
                f"삭제 항목이 너무 많습니다 ({to_delete}개). "
                f"최대 {MAX_DELETE_LIMIT}개까지 허용됩니다."
            )
        
        return errors
        
    except Exception as e:
        log(f"⚠️ 안전장치 검증 중 에러 (계속 진행): {e}")
        return errors


async def collect_agent_knowledge(agent_id: str) -> Dict:
    """에이전트 지식 수집 (의존성 체크용)"""
    from core.batch_deduplicator import collect_agent_knowledge as _collect
    return await _collect(agent_id)

