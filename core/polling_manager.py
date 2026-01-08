import asyncio
import os
from typing import Dict, List
from utils.logger import log, handle_error
from core.database import initialize_db, fetch_feedback_task, _get_agent_by_id, update_feedback_status
from core.feedback_processor import match_feedback_to_agents
from core.react_agent import process_feedback_with_react

def initialize_connections():
    """데이터베이스 연결 초기화"""
    try:
        initialize_db()
        log("연결 초기화 완료")
    except Exception as e:
        handle_error("초기화", e)

# ============================================================================
# 에이전트 정보 조회
# ============================================================================

async def get_agents_info(user_ids: str) -> List[Dict]:
    """콤마로 구분된 user_id들로 에이전트 정보 조회"""
    agent_list = []
    if not user_ids:
        return agent_list
        
    ids = [uid.strip() for uid in user_ids.split(',')]
    for agent_id in ids:
        agent_info = _get_agent_by_id(agent_id)
        if agent_info:
            agent_list.append(agent_info)
    
    return agent_list

# ============================================================================
# 피드백 작업 처리
# ============================================================================

async def process_feedback_task(row: Dict):
    """피드백 작업 처리"""
    todo_id = row['id']
    user_ids = row.get('user_id', '')
    feedback = row.get('feedback', '')
    description = row.get('description', '')  # 작업지시사항 추가
    
    try:
        # 피드백 처리 시작 - 상태를 PROCESSING으로 변경
        await update_feedback_status(todo_id, 'PROCESSING')
        log(f"피드백 작업 처리 시작: id={todo_id}")
        
        # 1. 에이전트 정보 조회
        agents = await get_agents_info(user_ids)
        if not agents:
            log(f"에이전트 정보를 찾을 수 없음: user_ids={user_ids}")
            return
            
        log(f"에이전트 {len(agents)}명 조회 완료")
        
        # 2. AI로 피드백 매칭 (작업지시사항 포함) - 학습 후보 생성
        matching_result = await match_feedback_to_agents(feedback, agents, description)
        agent_feedbacks = matching_result.get('agent_feedbacks', [])
        
        if not agent_feedbacks:
            log("매칭된 피드백이 없음")
            return
            
        log(f"학습 후보 생성 완료: {len(agent_feedbacks)}개")
        
        # 3. 각 학습 후보를 ReAct 에이전트로 처리 (기존 지식 통합 + 충돌 분석 + 분류 + 저장)
        for feedback_item in agent_feedbacks:
            agent_id = feedback_item.get('agent_id')
            agent_name = feedback_item.get('agent_name', 'Unknown')
            learning_candidate = feedback_item.get('learning_candidate', {})
            
            if not learning_candidate:
                log(f"⚠️ 에이전트 {agent_name}의 학습 후보가 비어있음, 건너뜀")
                continue
            
            # 에이전트 정보 조회
            agent_info = _get_agent_by_id(agent_id)
            if not agent_info:
                log(f"⚠️ 에이전트 정보를 찾을 수 없음: {agent_id}")
                continue
            
            # ReAct 에이전트로 피드백 처리
            feedback_content = learning_candidate.get('content', '')
            
            try:
                # ReAct 에이전트 방식 (Thought → Action → Observation)
                log(f"🤖 ReAct 에이전트로 피드백 처리: {agent_name}")
                result = await process_feedback_with_react(
                    agent_id=agent_id,
                    agent_info=agent_info,
                    feedback_content=feedback_content,
                    task_description=description
                )
                if result.get("error"):
                    log(f"⚠️ 에이전트 {agent_name}: 피드백 처리 중 에러 발생 (계속 진행): {result.get('error')[:200]}...")
            except Exception as feedback_error:
                # 개별 피드백 처리 실패 시에도 계속 진행
                log(f"⚠️ 에이전트 {agent_name}의 피드백 처리 실패 (계속 진행): {str(feedback_error)[:200]}...")
                handle_error(f"피드백처리({agent_name})", feedback_error)
                # 에러를 다시 발생시키지 않고 다음 피드백으로 진행
                continue
        
        log(f"피드백 작업 처리 완료: id={todo_id}")
        # 피드백 처리 완료 - 상태를 COMPLETED로 변경
        await update_feedback_status(todo_id, 'COMPLETED')
        
    except Exception as e:
        # 피드백 작업 처리 실패 시에도 폴링 계속 진행
        log(f"⚠️ 피드백 작업 처리 중 에러 발생 (폴링 계속 진행): {str(e)[:200]}...")
        handle_error("피드백작업처리", e)
        # 에러 발생 시에도 상태 업데이트 시도 (실패해도 계속 진행)
        try:
            await update_feedback_status(todo_id, 'FAILED')
        except:
            pass
        # 에러를 다시 발생시키지 않음 (폴링이 계속되도록)

# ============================================================================
# 폴링 실행
# ============================================================================

async def start_feedback_polling(interval: int = 7):
    """피드백 작업 폴링 시작"""
    log("피드백 작업 폴링 시작")
    
    while True:
        try:
            row = await fetch_feedback_task()
            if row:
                await process_feedback_task(row)
                
        except Exception as e:
            # 폴링 중 에러 발생 시에도 계속 진행
            log(f"⚠️ 폴링 중 에러 발생 (계속 진행): {str(e)[:200]}...")
            handle_error("폴링실행", e)
            # 에러를 다시 발생시키지 않음 (폴링이 계속되도록)
            
        await asyncio.sleep(interval)