import asyncio
from typing import Dict, List
from utils.logger import log, handle_error
from core.database import initialize_db, fetch_feedback_task, _get_agent_by_id
from core.feedback_processor import match_feedback_to_agents, save_to_mem0

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
        log(f"피드백 작업 처리 시작: id={todo_id}")
        
        # 1. 에이전트 정보 조회
        agents = await get_agents_info(user_ids)
        if not agents:
            log(f"에이전트 정보를 찾을 수 없음: user_ids={user_ids}")
            return
            
        log(f"에이전트 {len(agents)}명 조회 완료")
        
        # 2. AI로 피드백 매칭 (작업지시사항 포함)
        matching_result = await match_feedback_to_agents(feedback, agents, description)
        agent_feedbacks = matching_result.get('agent_feedbacks', [])
        
        if not agent_feedbacks:
            log("매칭된 피드백이 없음")
            return
            
        log(f"피드백 매칭 완료: {len(agent_feedbacks)}개")
        
        # 3. Mem0에 학습 저장
        await save_to_mem0(agent_feedbacks)
        
        log(f"피드백 작업 처리 완료: id={todo_id}")
        
    except Exception as e:
        handle_error("피드백작업처리", e)

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
            handle_error("폴링실행", e)
            
        await asyncio.sleep(interval)