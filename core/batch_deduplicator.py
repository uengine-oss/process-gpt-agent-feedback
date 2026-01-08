"""
배치 중복 제거 실행 모듈
에이전트별로 모든 지식을 수집하고 중복을 제거하는 배치 처리
"""

import uuid
from datetime import datetime
from typing import Dict, List, Optional
from utils.logger import log, handle_error
from core.database import get_all_agents, _get_agent_by_id, get_db_client
from core.knowledge_retriever import (
    get_memories_by_agent,
    retrieve_existing_dmn_rules,
    retrieve_existing_skills
)
from core.batch_analyzer import generate_deduplication_plan
from core.batch_validator import validate_batch_plan
from core.learning_committers.memory_committer import commit_to_memory
from core.learning_committers.dmn_committer import commit_to_dmn_rule
from core.learning_committers.skill_committer import commit_to_skill


async def collect_agent_knowledge(agent_id: str) -> Dict:
    """
    에이전트의 모든 지식 수집
    
    Args:
        agent_id: 에이전트 ID
    
    Returns:
        {
            "memories": [...],
            "dmn_rules": [...],
            "skills": [...]
        }
    """
    try:
        log(f"📦 에이전트 지식 수집 시작: agent_id={agent_id}")
        
        # 에이전트 정보 조회 (tenant_id, agent_skills 필요)
        agent_info = _get_agent_by_id(agent_id)
        if not agent_info:
            log(f"⚠️ 에이전트 정보를 찾을 수 없음: {agent_id}")
            return {
                "memories": [],
                "dmn_rules": [],
                "skills": []
            }
        
        tenant_id = agent_info.get("tenant_id")
        agent_skills = agent_info.get("skills")
        
        # 각 저장소에서 모든 지식 조회
        # MEMORY: get_memories_by_agent 사용 (limit을 크게 설정)
        memories_raw = await get_memories_by_agent(agent_id, limit=1000)
        # memories는 {"id": "...", "memory": "...", "metadata": {...}} 형식
        memories = []
        for mem in memories_raw:
            memories.append({
                "id": mem.get("id", ""),
                "memory": mem.get("memory", ""),
                "content": mem.get("memory", ""),
                "metadata": mem.get("metadata", {})
            })
        
        # DMN_RULE: retrieve_existing_dmn_rules 사용 (search_text 없이 모든 규칙 조회)
        dmn_rules = await retrieve_existing_dmn_rules(agent_id, search_text="")
        
        # SKILL: retrieve_existing_skills 사용 (agent_skills를 기반으로 조회)
        # 배치 작업에서는 업로드된 스킬(HTTP API로 조회 가능한 스킬)만 중복 분석 대상
        # 기본 내장 스킬은 중복 분석 대상에서 제외
        # skip_detail_fetch=False로 설정하여 전체 마크다운 내용 포함 (중복 분석 정확도 향상)
        skills = await retrieve_existing_skills(
            agent_id,
            search_text="",
            top_k=1000,
            tenant_id=tenant_id,
            agent_skills=agent_skills,
            only_uploaded_skills=True,  # 업로드된 스킬만 조회 (기본 내장 스킬 제외)
            skip_detail_fetch=False  # 상세 내용 포함 (전체 마크다운 내용 확보)
        )
        
        log(f"📊 에이전트 지식 수집 완료: agent_id={agent_id}, memories={len(memories)}, dmn_rules={len(dmn_rules)}, skills={len(skills)}")
        
        return {
            "memories": memories,
            "dmn_rules": dmn_rules,
            "skills": skills
        }
        
    except Exception as e:
        handle_error("에이전트지식수집", e)
        return {
            "memories": [],
            "dmn_rules": [],
            "skills": []
        }


async def execute_deduplication_plan(agent_id: str, plan: Dict, dry_run: bool = False, job_id: Optional[str] = None) -> Dict:
    """
    중복 제거 계획 실행
    
    Args:
        agent_id: 에이전트 ID
        plan: generate_deduplication_plan()의 결과
        dry_run: True면 실제 실행하지 않고 계획만 반환
    
    Returns:
        실행 결과
    """
    try:
        actions = plan.get("actions", [])
        
        if dry_run:
            log(f"🔍 DRY_RUN 모드: 실행 계획만 확인 (실제 실행 안 함)")
            log(f"   총 {len(actions)}개 작업: 삭제={plan.get('summary', {}).get('to_delete', 0)}, 유지={plan.get('summary', {}).get('to_keep', 0)}")
            return {
                "dry_run": True,
                "actions_count": len(actions),
                "to_delete": plan.get("summary", {}).get("to_delete", 0),
                "to_keep": plan.get("summary", {}).get("to_keep", 0),
                "plan": plan
            }
        
        # 실제 실행
        log(f"🔄 중복 제거 실행 시작: agent_id={agent_id}, 총 {len(actions)}개 작업")
        
        deleted_count = 0
        moved_count = 0
        kept_count = 0
        errors = []
        backups = []  # 롤백용 백업 데이터
        
        # 백업 생성 (DRY_RUN이 아니고 job_id가 있는 경우)
        if not dry_run and job_id:
            backups = await _create_backups(agent_id, actions, job_id)
        
        for action in actions:
            operation = action.get("operation")
            storage = action.get("storage")
            item_id = action.get("id")
            
            if not item_id:
                continue
            
            try:
                if operation == "DELETE":
                    if storage == "MEMORY":
                        await commit_to_memory(
                            agent_id=agent_id,
                            content="",  # DELETE에는 content 불필요
                            source_type="batch_deduplication",
                            operation="DELETE",
                            memory_id=item_id
                        )
                        deleted_count += 1
                        log(f"   🗑️ MEMORY 삭제: id={item_id}")
                    
                    elif storage == "DMN_RULE":
                        # DELETE 작업에서는 dmn_artifact가 사용되지 않지만 함수 시그니처상 필요
                        # 함수 내부에서 operation이 DELETE이면 바로 return하므로 내용은 무관
                        await commit_to_dmn_rule(
                            agent_id=agent_id,
                            dmn_artifact={},  # DELETE에는 사용되지 않음
                            feedback_content="배치 중복 제거",
                            operation="DELETE",
                            rule_id=item_id
                        )
                        deleted_count += 1
                        log(f"   🗑️ DMN_RULE 삭제: id={item_id}")
                    
                    elif storage == "SKILL":
                        # SKILL의 경우 skill_id는 skill_name을 의미
                        await commit_to_skill(
                            agent_id=agent_id,
                            skill_artifact={},  # DELETE에는 artifact 불필요
                            operation="DELETE",
                            skill_id=item_id,
                            feedback_content="배치 중복 제거"
                        )
                        deleted_count += 1
                        log(f"   🗑️ SKILL 삭제: id={item_id}")
                
                elif operation == "MOVE":
                    # 저장소 간 이동: 원본 삭제 + 대상 저장소에 생성
                    from_storage = action.get("from_storage")
                    to_storage = action.get("to_storage")
                    full_content = action.get("full_content", "")
                    content_summary = action.get("content_summary", "")
                    
                    log(f"   🔄 {from_storage} -> {to_storage} 이동 시작: id={item_id}")
                    
                    # 원본 지식 항목 찾기
                    original_item = None
                    knowledge = await collect_agent_knowledge(agent_id)
                    
                    if from_storage == "MEMORY":
                        original_item = next((m for m in knowledge.get("memories", []) if m.get("id") == item_id), None)
                    elif from_storage == "DMN_RULE":
                        original_item = next((r for r in knowledge.get("dmn_rules", []) if r.get("id") == item_id), None)
                    elif from_storage == "SKILL":
                        skill_id = item_id
                        original_item = next((s for s in knowledge.get("skills", []) if (s.get("id") == skill_id or s.get("name") == skill_id)), None)
                    
                    if not original_item:
                        log(f"   ⚠️ 원본 항목을 찾을 수 없어 이동 건너뜀: {from_storage} id={item_id}")
                        errors.append(f"MOVE 실패: 원본 항목 없음 ({from_storage} id={item_id})")
                        continue
                    
                    try:
                        # 1. 대상 저장소에 생성
                        moved_to_id = None
                        if to_storage == "MEMORY":
                            # MEMORY로 이동: content 사용
                            content = full_content or original_item.get("memory") or original_item.get("content") or content_summary
                            result = await commit_to_memory(
                                agent_id=agent_id,
                                content=content,
                                source_type="batch_deduplication_move",
                                operation="CREATE"
                            )
                            # MEMORY는 생성 후 ID를 직접 얻을 수 없으므로 None 유지
                            log(f"   ✅ MEMORY 생성 완료 (이동)")
                            
                        elif to_storage == "DMN_RULE":
                            # DMN_RULE로 이동: LLM으로 condition과 action 추출 필요
                            from core.learning_router import route_learning
                            
                            content_for_extraction = full_content or original_item.get("bpmn") or original_item.get("memory") or original_item.get("content") or content_summary
                            
                            # route_learning을 사용하여 DMN 정보 추출
                            route_result = await route_learning({
                                "content": content_for_extraction,
                                "intent_hint": "조건-행동 규칙 추출"
                            })
                            
                            artifacts = route_result.get("artifacts", {})
                            dmn_artifact = artifacts.get("dmn", {})
                            
                            condition = dmn_artifact.get("condition", "")
                            action = dmn_artifact.get("action", "")
                            
                            if not condition or not action:
                                # 추출 실패 시 원본 내용을 기반으로 기본 규칙 생성
                                # 간단히 내용을 반으로 나눔 (개선 가능)
                                content_parts = content_for_extraction.split("\n")
                                mid_point = len(content_parts) // 2
                                condition = "\n".join(content_parts[:mid_point])[:500]
                                action = "\n".join(content_parts[mid_point:])[:500] if len(content_parts) > mid_point else content_for_extraction[:500]
                                log(f"   ⚠️ DMN 추출 실패, 기본값 사용")
                            
                            rule_name = original_item.get("name") or dmn_artifact.get("name") or f"이동된 규칙 {item_id[:8]}"
                            
                            # DMN_RULE 생성 (ID는 내부에서 생성됨)
                            try:
                                await commit_to_dmn_rule(
                                    agent_id=agent_id,
                                    dmn_artifact={
                                        "condition": condition,
                                        "action": action,
                                        "name": rule_name
                                    },
                                    feedback_content=f"배치 중복 제거: {from_storage}에서 이동",
                                    operation="CREATE"
                                )
                                # DMN_RULE은 생성 후 ID를 직접 얻을 수 없으므로 None 유지
                                log(f"   ✅ DMN_RULE 생성 완료 (이동)")
                            except Exception as dmn_error:
                                # DMN Rule validation 실패 (condition/action 누락 등)인 경우
                                msg = str(dmn_error)
                                if "DMN Rule의 condition과 action은 필수입니다" in msg:
                                    log("   ⚠️ DMN_RULE 생성 실패(조건/액션 누락) → 이동 대신 원본 지식만 삭제 대상으로 처리")
                                    # 아래 공통 삭제 로직에서 처리하도록 넘어감
                                else:
                                    # 다른 에러는 그대로 상위 MOVE 예외 처리로 위임
                                    raise
                            
                        elif to_storage == "SKILL":
                            # SKILL로 이동: steps 추출 필요
                            from core.learning_router import route_learning
                            
                            content_for_extraction = full_content or original_item.get("content") or original_item.get("memory") or content_summary
                            
                            # route_learning을 사용하여 SKILL 정보 추출
                            route_result = await route_learning({
                                "content": content_for_extraction,
                                "intent_hint": "단계별 절차 추출"
                            })
                            
                            artifacts = route_result.get("artifacts", {})
                            skill_artifact = artifacts.get("skill", {})
                            
                            steps = skill_artifact.get("steps", [])
                            
                            if not steps:
                                # 추출 실패 시 원본 내용을 단일 step으로 사용
                                steps = [content_for_extraction[:500]]
                                log(f"   ⚠️ SKILL steps 추출 실패, 기본값 사용")
                            
                            skill_name = original_item.get("name") or original_item.get("id") or skill_artifact.get("name") or f"이동된 스킬 {item_id[:8]}"
                            description = skill_artifact.get("description") or original_item.get("description") or content_summary[:200]
                            
                            await commit_to_skill(
                                agent_id=agent_id,
                                skill_artifact={
                                    "name": skill_name,
                                    "description": description,
                                    "steps": steps,
                                    "overview": skill_artifact.get("overview"),
                                    "usage": skill_artifact.get("usage")
                                },
                                operation="CREATE",
                                feedback_content=f"배치 중복 제거: {from_storage}에서 이동"
                            )
                            # SKILL의 경우 skill_name이 ID 역할
                            moved_to_id = skill_name
                            log(f"   ✅ SKILL 생성 완료 (이동)")
                        
                        # 2. 원본 삭제 (DMN 생성 성공/실패와 상관없이, 이 MOVE가 유효하다고 판단된 경우에는 원본은 제거)
                        if from_storage == "MEMORY":
                            await commit_to_memory(
                                agent_id=agent_id,
                                content="",
                                source_type="batch_deduplication_move",
                                operation="DELETE",
                                memory_id=item_id
                            )
                        elif from_storage == "DMN_RULE":
                            await commit_to_dmn_rule(
                                agent_id=agent_id,
                                dmn_artifact={},
                                feedback_content=f"배치 중복 제거: {to_storage}로 이동",
                                operation="DELETE",
                                rule_id=item_id
                            )
                        elif from_storage == "SKILL":
                            await commit_to_skill(
                                agent_id=agent_id,
                                skill_artifact={},
                                operation="DELETE",
                                skill_id=item_id,
                                feedback_content=f"배치 중복 제거: {to_storage}로 이동"
                            )
                        
                        # MOVE 작업 변경 이력 기록
                        try:
                            from core.database import record_knowledge_history, _get_agent_by_id
                            agent_info = _get_agent_by_id(agent_id)
                            tenant_id = agent_info.get("tenant_id") if agent_info else None
                            
                            # 원본 항목의 이름 추출
                            original_name = original_item.get("name") or item_id
                            
                            record_knowledge_history(
                                knowledge_type=from_storage,
                                knowledge_id=item_id,
                                agent_id=agent_id,
                                tenant_id=tenant_id,
                                operation="MOVE",
                                previous_content=original_item,
                                new_content={"moved_to": to_storage, "moved_to_id": moved_to_id} if moved_to_id else {"moved_to": to_storage},
                                feedback_content=f"배치 중복 제거: {from_storage}에서 {to_storage}로 이동",
                                knowledge_name=original_name if from_storage != "MEMORY" else None,
                                moved_from_storage=from_storage,
                                moved_to_storage=to_storage,
                                batch_job_id=job_id
                            )
                        except Exception as e:
                            log(f"   ⚠️ MOVE 변경 이력 기록 실패 (계속 진행): {e}")
                        
                        # 백업에 moved_to_id 업데이트 (job_id가 있는 경우)
                        if job_id and moved_to_id:
                            try:
                                supabase = get_db_client()
                                supabase.table("batch_job_backup").update({
                                    "moved_to_id": moved_to_id
                                }).eq("job_id", job_id).eq("item_id", item_id).eq("agent_id", agent_id).execute()
                            except Exception as e:
                                log(f"   ⚠️ 백업 업데이트 실패 (계속 진행): {e}")
                        
                        moved_count += 1
                        log(f"   ✅ {from_storage} -> {to_storage} 이동 완료: id={item_id}")
                        
                    except Exception as move_error:
                        msg = str(move_error)
                        # DMN Rule validation 오류인 경우: DMN 생성은 포기하고 원본 지식만 삭제 대상으로 처리
                        if to_storage == "DMN_RULE" and "DMN Rule의 condition과 action은 필수입니다" in msg:
                            log(f"   ⚠️ DMN_RULE 생성 validation 실패, 이동 대신 원본 지식만 삭제 처리: {from_storage} id={item_id}")
                            try:
                                if from_storage == "MEMORY":
                                    await commit_to_memory(
                                        agent_id=agent_id,
                                        content="",
                                        source_type="batch_deduplication_move_invalid_dmn",
                                        operation="DELETE",
                                        memory_id=item_id
                                    )
                                elif from_storage == "DMN_RULE":
                                    await commit_to_dmn_rule(
                                        agent_id=agent_id,
                                        dmn_artifact={},
                                        feedback_content="배치 중복 제거: 잘못된 DMN 규칙 삭제",
                                        operation="DELETE",
                                        rule_id=item_id
                                    )
                                elif from_storage == "SKILL":
                                    await commit_to_skill(
                                        agent_id=agent_id,
                                        skill_artifact={},
                                        operation="DELETE",
                                        skill_id=item_id,
                                        feedback_content="배치 중복 제거: 잘못된 DMN 규칙 이동 실패로 원본 스킬 삭제"
                                    )
                                deleted_count += 1
                                log(f"   ✅ DMN validation 실패로 원본 지식 삭제 완료: {from_storage} id={item_id}")
                            except Exception as delete_err:
                                # 삭제까지 실패하면 그때만 에러로 올림
                                error_msg = f"MOVE->DELETE fallback 실패 ({from_storage} -> DMN_RULE, id={item_id}): {delete_err}"
                                errors.append(error_msg)
                                log(f"   ⚠️ {error_msg}")
                                handle_error(f"배치중복제거실행_MOVE_FALLBACK_{from_storage}_DMN_RULE", delete_err)
                        else:
                            error_msg = f"MOVE 실패 ({from_storage} -> {to_storage}, id={item_id}): {move_error}"
                            errors.append(error_msg)
                            log(f"   ⚠️ {error_msg}")
                            handle_error(f"배치중복제거실행_MOVE_{from_storage}_{to_storage}", move_error)
                
                elif operation == "KEEP":
                    kept_count += 1
                    log(f"   ✅ {storage} 유지: id={item_id}")
                
            except Exception as e:
                error_msg = f"{storage} {operation} 실패 (id={item_id}): {e}"
                errors.append(error_msg)
                log(f"   ⚠️ {error_msg}")
                handle_error(f"배치중복제거실행_{storage}_{operation}", e)
        
        log(f"✅ 중복 제거 실행 완료: agent_id={agent_id}, 삭제={deleted_count}, 이동={moved_count}, 유지={kept_count}, 에러={len(errors)}")
        
        return {
            "dry_run": False,
            "deleted_count": deleted_count,
            "moved_count": moved_count,
            "kept_count": kept_count,
            "errors": errors,
            "plan": plan,
            "backups_created": len(backups)
        }
        
    except Exception as e:
        handle_error("중복제거계획실행", e)
        raise


async def _create_backups(agent_id: str, actions: List[Dict], job_id: str) -> List[Dict]:
    """
    배치 작업 실행 전 백업 생성 (롤백용)
    
    Args:
        agent_id: 에이전트 ID
        actions: 실행할 작업 목록
        job_id: 배치 작업 ID
    
    Returns:
        생성된 백업 목록
    """
    backups = []
    supabase = get_db_client()
    
    # tenant_id 조회 (batch_job_backup 테이블의 복합 FK를 맞추기 위해)
    tenant_id = None
    try:
        agent_info = _get_agent_by_id(agent_id)
        if agent_info:
            tenant_id = agent_info.get("tenant_id")
    except Exception as e:
        log(f"⚠️ 백업 생성 시 tenant_id 조회 실패 (계속 진행): {e}")
    
    try:
        # DELETE 및 MOVE 작업에 대해 백업 생성
        for action in actions:
            operation = action.get("operation")
            if operation not in ["DELETE", "MOVE"]:
                continue
            
            storage = action.get("storage")
            from_storage = action.get("from_storage", storage)
            item_id = action.get("id")
            
            if not item_id:
                continue
            
            # 원본 항목 조회
            knowledge = await collect_agent_knowledge(agent_id)
            original_item = None
            
            if from_storage == "MEMORY":
                original_item = next((m for m in knowledge.get("memories", []) if m.get("id") == item_id), None)
            elif from_storage == "DMN_RULE":
                original_item = next((r for r in knowledge.get("dmn_rules", []) if r.get("id") == item_id), None)
            elif from_storage == "SKILL":
                skill_id = item_id
                original_item = next((s for s in knowledge.get("skills", []) if (s.get("id") == skill_id or s.get("name") == skill_id)), None)
            
            if not original_item:
                log(f"   ⚠️ 백업 생성 실패: 원본 항목 없음 ({from_storage} id={item_id})")
                continue
            
            # 백업 데이터 구성
            backup_data = {
                "job_id": job_id,
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "storage_type": from_storage,
                "item_id": item_id,
                "operation": operation,
                "original_content": original_item  # JSONB로 저장 (Supabase가 자동 변환)
            }
            
            # MOVE인 경우 이동 정보 추가
            if operation == "MOVE":
                to_storage = action.get("to_storage")
                backup_data["moved_to_storage"] = to_storage
                # moved_to_id는 나중에 생성된 후 업데이트 필요 (임시로 None)
                backup_data["moved_to_id"] = None
            
            # 데이터베이스에 저장
            try:
                supabase.table("batch_job_backup").insert(backup_data).execute()
                backups.append(backup_data)
                log(f"   💾 백업 생성: {from_storage} id={item_id}")
            except Exception as e:
                log(f"   ⚠️ 백업 저장 실패 ({from_storage} id={item_id}): {e}")
        
    except Exception as e:
        log(f"⚠️ 백업 생성 중 에러 (계속 진행): {e}")
        handle_error("배치백업생성", e)
    
    return backups


async def process_agent(agent_id: str, dry_run: bool = False, job_id: Optional[str] = None) -> Dict:
    """
    특정 에이전트의 중복 제거 처리
    
    Args:
        agent_id: 에이전트 ID
        dry_run: True면 실제 실행하지 않고 계획만 반환
        job_id: 배치 작업 ID (롤백용)
    
    Returns:
        처리 결과
    """
    try:
        log(f"🔄 에이전트 배치 처리 시작: agent_id={agent_id}, dry_run={dry_run}")
        
        # 1. 지식 수집
        knowledge = await collect_agent_knowledge(agent_id)
        
        memories = knowledge.get("memories", [])
        dmn_rules = knowledge.get("dmn_rules", [])
        skills = knowledge.get("skills", [])
        
        # 지식이 없으면 건너뛰기
        if not memories and not dmn_rules and not skills:
            log(f"📝 에이전트 {agent_id}에 지식이 없어 건너뜀")
            return {
                "agent_id": agent_id,
                "skipped": True,
                "reason": "지식 없음"
            }
        
        # 2. 중복 분석 및 계획 생성
        plan = await generate_deduplication_plan(agent_id, memories, dmn_rules, skills)
        
        # 3. 계획 검증 (DRY_RUN이 아닌 경우)
        if not dry_run:
            validation = await validate_batch_plan(agent_id, plan)
            if not validation.get("valid"):
                errors = validation.get("errors", [])
                warnings = validation.get("warnings", [])
                log(f"⚠️ 배치 계획 검증 실패: 에러={len(errors)}, 경고={len(warnings)}")
                if errors:
                    return {
                        "agent_id": agent_id,
                        "skipped": True,
                        "reason": "검증 실패",
                        "validation_errors": errors,
                        "validation_warnings": warnings
                    }
                if warnings:
                    log(f"⚠️ 경고 사항: {warnings}")
        
        # 4. 계획 실행
        result = await execute_deduplication_plan(agent_id, plan, dry_run=dry_run, job_id=job_id)
        
        result["agent_id"] = agent_id
        result["skipped"] = False
        
        return result
        
    except Exception as e:
        handle_error(f"에이전트배치처리_{agent_id}", e)
        return {
            "agent_id": agent_id,
            "skipped": False,
            "error": str(e)
        }


class BatchDeduplicator:
    """배치 중복 제거 클래스"""
    
    async def execute_batch_deduplication(
        self,
        agent_id: Optional[str] = None,
        dry_run: bool = False,
        job_id: Optional[str] = None
    ) -> Dict:
        """
        배치 중복 제거 실행
        
        Args:
            agent_id: 특정 에이전트만 처리 (필수, 에이전트별 배치만 지원)
            dry_run: True면 실제 실행하지 않고 계획만 반환
            job_id: 배치 작업 ID (롤백용, None이면 자동 생성)
        
        Returns:
            처리 결과
        """
        try:
            # 에이전트별 배치만 지원
            if not agent_id:
                raise ValueError("agent_id는 필수입니다. 에이전트별 배치만 지원합니다.")

            # 배치 작업 ID 생성
            if not job_id and not dry_run:
                job_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            
            log(f"🚀 배치 중복 제거 시작: agent_id={agent_id}, dry_run={dry_run}, job_id={job_id}")
            
            # 배치 작업 이력 기록 시작
            if job_id and not dry_run:
                await _record_batch_job_start(job_id, agent_id, dry_run)
            
            # 에이전트 정보 조회 (tenant_id 확보용)
            agent_info = _get_agent_by_id(agent_id)
            if not agent_info:
                log(f"⚠️ 에이전트를 찾을 수 없음: {agent_id}")
                return {
                    "success": False,
                    "error": f"에이전트를 찾을 수 없음: {agent_id}",
                    "processed_agents": 0,
                    "results": []
                }
            tenant_id_for_job = agent_info.get("tenant_id")
            
            # 에이전트 한 명 처리
            try:
                result = await process_agent(agent_id, dry_run=dry_run, job_id=job_id)
            except Exception as e:
                log(f"⚠️ 에이전트 {agent_id} 처리 실패: {e}")
                handle_error(f"배치처리_{agent_id}", e)
                result = {
                    "agent_id": agent_id,
                    "skipped": False,
                    "error": str(e)
                }
            
            results = [result]

            # 요약
            is_skipped = result.get("skipped", False)
            total_processed = 0 if is_skipped else 1
            total_deleted = result.get("deleted_count", 0) if not result.get("dry_run") else 0
            total_moved = result.get("moved_count", 0) if not result.get("dry_run") else 0
            total_kept = result.get("kept_count", 0) if not result.get("dry_run") else 0
            total_errors = len(result.get("errors", [])) if "errors" in result else 0
            
            # 배치 작업 이력 기록 완료
            if job_id and not dry_run:
                # plan/actions 기반으로 조금 더 풍부한 요약 생성
                plan = result.get("plan") or {}
                actions = plan.get("actions", [])

                deleted_items = [
                    {
                        "storage": a.get("storage"),
                        "id": a.get("id"),
                        "reason": a.get("reason"),
                        "content_summary": a.get("content_summary"),
                    }
                    for a in actions
                    if a.get("operation") == "DELETE"
                ]
                moved_items = [
                    {
                        "from_storage": a.get("from_storage"),
                        "to_storage": a.get("to_storage"),
                        "id": a.get("id"),
                        "reason": a.get("reason"),
                        "content_summary": a.get("content_summary"),
                    }
                    for a in actions
                    if a.get("operation") == "MOVE"
                ]

                kept_by_storage: Dict[str, int] = {}
                for a in actions:
                    if a.get("operation") == "KEEP":
                        storage = a.get("storage")
                        if storage:
                            kept_by_storage[storage] = kept_by_storage.get(storage, 0) + 1

                db_summary = {
                    "agent_id": result.get("agent_id"),
                    "skipped": result.get("skipped", False),
                    "reason": result.get("reason"),
                    "deleted_count": total_deleted,
                    "moved_count": total_moved,
                    "kept_count": total_kept,
                    "errors_count": total_errors,
                    "dry_run": result.get("dry_run", False),
                    "total_knowledge_count": plan.get("total_knowledge_count", {}),
                    "deleted_items": deleted_items,
                    "moved_items": moved_items,
                    "kept_by_storage": kept_by_storage,
                }

                await _record_batch_job_complete(
                    job_id, "COMPLETED", 1, total_processed,
                    total_deleted, total_moved, total_kept, total_errors,
                    db_summary,
                    agent_id=agent_id,
                    tenant_id=tenant_id_for_job,
                )
            
            summary = {
                "success": True,
                "dry_run": dry_run,
                "job_id": job_id,
                "total_agents": 1,
                "processed_agents": total_processed,
                "total_deleted": total_deleted,
                "total_moved": total_moved,
                "total_kept": total_kept,
                "total_errors": total_errors,
                "results": results
            }
            
            log(f"✅ 배치 중복 제거 완료 (에이전트별): agent_id={agent_id}, 처리={total_processed}, 삭제={total_deleted}, 이동={total_moved}, 유지={total_kept}, 에러={total_errors}")
            
            return summary
            
        except Exception as e:
            error_msg = str(e)
            handle_error("배치중복제거실행", e)
            
            # 에러 기록
            if job_id and not dry_run:
                await _record_batch_job_complete(
                    job_id, "FAILED", 0, 0, 0, 0, 0, 0,
                    {"error": error_msg}
                )
            
            return {
                "success": False,
                "error": error_msg,
                "job_id": job_id,
                "processed_agents": []
            }


async def _record_batch_job_start(job_id: str, agent_id: Optional[str], dry_run: bool) -> None:
    """배치 작업 시작 기록"""
    try:
        supabase = get_db_client()
        tenant_id = None
        if agent_id:
            try:
                from core.database import _get_agent_by_id as _get_agent_for_batch
                agent_info = _get_agent_for_batch(agent_id)
                if agent_info:
                    tenant_id = agent_info.get("tenant_id")
            except Exception as e:
                log(f"⚠️ 배치 작업 시작 시 tenant_id 조회 실패 (계속 진행): {e}")

        supabase.table("batch_job_history").insert({
            "job_id": job_id,
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "started_at": datetime.now().isoformat(),
            "status": "RUNNING",
            "dry_run": dry_run,
            "total_agents": 0,
            "processed_agents": 0
        }).execute()
        log(f"📝 배치 작업 시작 기록: job_id={job_id}")
    except Exception as e:
        log(f"⚠️ 배치 작업 시작 기록 실패 (계속 진행): {e}")


async def _record_batch_job_complete(
    job_id: str,
    status: str,
    total_agents: int,
    processed_agents: int,
    total_deleted: int,
    total_moved: int,
    total_kept: int,
    total_errors: int,
    summary: Dict,
    agent_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """배치 작업 완료 기록"""
    try:
        import json
        supabase = get_db_client()
        
        # 먼저 레코드가 존재하는지 확인
        existing = supabase.table("batch_job_history").select("job_id").eq("job_id", job_id).execute()
        if not existing.data:
            # 레코드가 없으면 새로 생성
            log(f"⚠️ 배치 작업 레코드가 없어 새로 생성: job_id={job_id}")
            supabase.table("batch_job_history").insert({
                "job_id": job_id,
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "status": status,
                "started_at": datetime.now().isoformat(),
                "completed_at": datetime.now().isoformat(),
                "total_agents": total_agents,
                "processed_agents": processed_agents,
                "total_deleted": total_deleted,
                "total_moved": total_moved,
                "total_kept": total_kept,
                "total_errors": total_errors,
                "summary": summary  # Supabase가 자동으로 JSONB로 변환
            }).execute()
        else:
            # 레코드가 있으면 업데이트
            update_data = {
                "status": status,
                "completed_at": datetime.now().isoformat(),
                "total_agents": total_agents,
                "processed_agents": processed_agents,
                "total_deleted": total_deleted,
                "total_moved": total_moved,
                "total_kept": total_kept,
                "total_errors": total_errors,
                "summary": summary  # Supabase가 자동으로 JSONB로 변환
            }
            supabase.table("batch_job_history").update(update_data).eq("job_id", job_id).execute()
        
        log(f"📝 배치 작업 완료 기록: job_id={job_id}, status={status}")
    except Exception as e:
        import traceback
        log(f"⚠️ 배치 작업 완료 기록 실패 (계속 진행): {e}")
        log(f"   상세 에러: {traceback.format_exc()}")

