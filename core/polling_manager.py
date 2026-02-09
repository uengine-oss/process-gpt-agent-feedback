import asyncio
import json
import os
from typing import Dict, List, Any, Optional
from utils.logger import log, handle_error
from core.database import (
    initialize_db,
    fetch_feedback_task,
    _get_agent_by_id,
    update_feedback_status,
    fetch_events_by_todo_id,
)
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

async def get_agents_info(user_ids: str, assignees: Optional[Any] = None) -> List[Dict]:
    """
    콤마로 구분된 user_id들로 에이전트 정보 조회
    user_id로 에이전트를 찾지 못하면 assignees 컬럼의 endpoint에서 에이전트 ID를 찾음
    
    Args:
        user_ids: 콤마로 구분된 user_id 문자열
        assignees: assignees 컬럼 값 (JSON 배열, 선택적)
    
    Returns:
        에이전트 정보 리스트
    """
    agent_list = []
    found_agent_ids = set()  # 중복 방지를 위한 set
    
    # 1. user_id로 에이전트 조회 시도
    if user_ids:
        ids = [uid.strip() for uid in user_ids.split(',')]
        for user_id in ids:
            agent_info = _get_agent_by_id(user_id)
            if agent_info:
                agent_list.append(agent_info)
                found_agent_ids.add(user_id)
                log(f"✅ user_id로 에이전트 찾음: {user_id}")
    
    # 2. user_id로 에이전트를 찾지 못한 경우, assignees에서 찾기
    if not agent_list and assignees:
        try:
            # assignees가 문자열인 경우 JSON 파싱
            if isinstance(assignees, str):
                assignees_data = json.loads(assignees)
            else:
                assignees_data = assignees
            
            # assignees가 배열인지 확인
            if isinstance(assignees_data, list):
                for assignee_item in assignees_data:
                    if isinstance(assignee_item, dict):
                        endpoint = assignee_item.get('endpoint', [])
                        if isinstance(endpoint, list):
                            # endpoint 배열의 각 ID를 확인
                            for endpoint_id in endpoint:
                                if endpoint_id and endpoint_id not in found_agent_ids:
                                    agent_info = _get_agent_by_id(endpoint_id)
                                    if agent_info:
                                        agent_list.append(agent_info)
                                        found_agent_ids.add(endpoint_id)
                                        log(f"✅ assignees의 endpoint에서 에이전트 찾음: {endpoint_id}")
        except Exception as e:
            log(f"⚠️ assignees 파싱 중 에러 발생 (무시하고 계속 진행): {str(e)[:200]}...")
            handle_error("assignees파싱", e)
    
    return agent_list

# ============================================================================
# 피드백 작업 처리
# ============================================================================

async def process_feedback_task(row: Dict):
    """피드백 작업 처리"""
    todo_id = row['id']
    user_ids = row.get('user_id', '')
    assignees = row.get('assignees', None)  # assignees 컬럼 추가
    feedback_raw = row.get('feedback', '')
    description = row.get('description', '')  # 작업지시사항 추가
    
    # feedback이 배열인 경우 최신순으로 정렬하여 가장 최근 피드백만 가져오기
    feedback = ''
    if isinstance(feedback_raw, list):
        # 배열인 경우: time 기준으로 최신순 정렬 후 가장 최근 피드백만 사용
        try:
            if len(feedback_raw) > 0:
                # time 기준으로 내림차순 정렬 (최신이 먼저)
                sorted_feedback = sorted(
                    feedback_raw,
                    key=lambda x: x.get('time', ''),
                    reverse=True
                )
                # 가장 최근 피드백의 content만 사용
                latest_feedback = sorted_feedback[0]
                if isinstance(latest_feedback, dict):
                    feedback = latest_feedback.get('content', '')
                    if not feedback:
                        log(f"⚠️ 최신 피드백의 content가 비어있음: {latest_feedback}")
                else:
                    feedback = str(latest_feedback)
        except Exception as e:
            log(f"⚠️ 피드백 배열 처리 중 에러 발생: {str(e)[:200]}...")
            # 에러 발생 시 빈 문자열로 처리
            feedback = ''
    elif isinstance(feedback_raw, str):
        # 이미 문자열인 경우 그대로 사용
        feedback = feedback_raw
    elif feedback_raw:
        # 다른 타입인 경우 문자열로 변환
        feedback = str(feedback_raw)
    
    try:
        # 피드백 처리 시작 - 상태를 PROCESSING으로 변경
        await update_feedback_status(todo_id, 'PROCESSING')
        log(f"피드백 작업 처리 시작: id={todo_id}")

        # 1. 에이전트 정보 조회 (user_id로 먼저 시도, 실패 시 assignees에서 찾기)
        agents = await get_agents_info(user_ids, assignees)
        if not agents:
            log(f"⚠️ 에이전트 정보를 찾을 수 없음: user_ids={user_ids}")
            # 에이전트를 찾을 수 없으면 상태를 FAILED로 변경하고 종료
            try:
                await update_feedback_status(todo_id, 'FAILED')
            except:
                pass
            return
            
        log(f"에이전트 {len(agents)}명 조회 완료")

        # 2. 해당 TODO의 이벤트 로그 조회 (실제 스킬/지식 사용 이력)
        events = await fetch_events_by_todo_id(todo_id)
        log(f"이벤트 로그 조회 완료: todo_id={todo_id}, count={len(events)}")

        # 3. AI로 피드백 매칭 (작업지시사항 + 이벤트 로그 포함) - 학습 후보 생성
        matching_result = await match_feedback_to_agents(
            feedback,
            agents,
            description,
            events,
        )
        agent_feedbacks = matching_result.get('agent_feedbacks', [])
        
        if not agent_feedbacks:
            log("⚠️ 매칭된 피드백이 없음")
            # 매칭된 피드백이 없으면 상태를 COMPLETED로 변경하고 종료 (정상 종료)
            try:
                await update_feedback_status(todo_id, 'COMPLETED')
            except:
                pass
            return
            
        log(f"학습 후보 생성 완료: {len(agent_feedbacks)}개")

        # 4. 각 학습 후보를 ReAct 에이전트로 처리
        #    (기존 지식 통합 + 충돌 분석 + 분류 + 저장, 이벤트 로그도 함께 전달)
        had_any_error = False
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
                    task_description=description,
                    events=events,
                )
                if result.get("error"):
                    had_any_error = True
                    log(f"⚠️ 에이전트 {agent_name}: 피드백 처리 중 에러 발생 (계속 진행): {result.get('error')[:200]}...")
            except Exception as feedback_error:
                # 개별 피드백 처리 실패 시에도 계속 진행
                had_any_error = True
                log(f"⚠️ 에이전트 {agent_name}의 피드백 처리 실패 (계속 진행): {str(feedback_error)[:200]}...")
                handle_error(f"피드백처리({agent_name})", feedback_error)
                # 에러를 다시 발생시키지 않고 다음 피드백으로 진행
                continue
        
        log(f"피드백 작업 처리 완료: id={todo_id}")
        # 피드백 처리 완료 - 에이전트 처리 중 에러/노커밋이 있었으면 FAILED로 기록
        try:
            await update_feedback_status(todo_id, 'FAILED' if had_any_error else 'COMPLETED')
        except Exception as status_error:
            log(f"⚠️ 상태 업데이트 실패 (무시하고 계속 진행): {str(status_error)[:200]}...")
            # 상태 업데이트 실패는 로깅만 하고 계속 진행
        
    except Exception as e:
        # 피드백 작업 처리 실패 시에도 폴링 계속 진행
        log(f"⚠️ 피드백 작업 처리 중 에러 발생 (폴링 계속 진행): {str(e)[:200]}...")
        handle_error("피드백작업처리", e)  # 기본적으로 예외를 발생시키지 않음
        # 에러 발생 시에도 상태 업데이트 시도 (실패해도 계속 진행)
        try:
            await update_feedback_status(todo_id, 'FAILED')
        except Exception as status_error:
            log(f"⚠️ 상태 업데이트 실패 (무시하고 계속 진행): {str(status_error)[:200]}...")
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
            else:
                # 작업이 없으면 잠시 대기 후 계속
                log(f"처리할 피드백 작업이 없음 (대기 중...)")
                
        except KeyboardInterrupt:
            # Ctrl+C 등으로 중단된 경우
            log("피드백 폴링이 중단되었습니다.")
            break
        except BaseExceptionGroup as eg:
            # ExceptionGroup 처리 (Python 3.11+)
            log(f"⚠️ 폴링 중 ExceptionGroup 발생 (계속 진행): {len(eg.exceptions)}개 예외")
            for exc in eg.exceptions:
                log(f"   - 예외: {type(exc).__name__}: {str(exc)[:200]}...")
                try:
                    handle_error("폴링실행_ExceptionGroup", exc)  # 기본적으로 예외를 발생시키지 않음
                except Exception:
                    pass  # handle_error 자체에서 예외가 발생해도 무시
        except Exception as e:
            # 폴링 중 에러 발생 시에도 계속 진행
            log(f"⚠️ 폴링 중 에러 발생 (계속 진행): {str(e)[:200]}...")
            try:
                handle_error("폴링실행", e)  # 기본적으로 예외를 발생시키지 않음
            except Exception:
                pass  # handle_error 자체에서 예외가 발생해도 무시
            
        try:
            await asyncio.sleep(interval)
        except Exception as e:
            # sleep 중 에러 발생 시에도 계속 진행
            log(f"⚠️ 폴링 대기 중 에러 발생 (계속 진행): {str(e)[:200]}...")
            await asyncio.sleep(interval)  # 다시 시도