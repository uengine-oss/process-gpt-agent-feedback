import json
from typing import Dict, List, Any, Optional
from utils.logger import log, handle_error
from core.database import (
    initialize_db,
    extract_new_feedback_items,
    mark_feedback_collected_count,
    _get_agent_by_id,
    update_feedback_status,
    fetch_events_by_todo_id,
)
from core.feedback_processor import match_feedback_to_agents
from core.deep_agent import process_feedback_with_deep_agent

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
    """콤마로 구분된 user_id들로 에이전트 정보 조회"""
    agent_list = []
    found_agent_ids = set()

    if user_ids:
        ids = [uid.strip() for uid in user_ids.split(',')]
        for user_id in ids:
            agent_info = _get_agent_by_id(user_id)
            if agent_info:
                agent_list.append(agent_info)
                found_agent_ids.add(user_id)
                log(f"✅ user_id로 에이전트 찾음: {user_id}")

    if not agent_list and assignees:
        try:
            if isinstance(assignees, str):
                assignees_data = json.loads(assignees)
            else:
                assignees_data = assignees

            if isinstance(assignees_data, list):
                for assignee_item in assignees_data:
                    if isinstance(assignee_item, dict):
                        endpoint = assignee_item.get('endpoint', [])
                        if isinstance(endpoint, list):
                            for endpoint_id in endpoint:
                                if endpoint_id and endpoint_id not in found_agent_ids:
                                    agent_info = _get_agent_by_id(endpoint_id)
                                    if agent_info:
                                        agent_list.append(agent_info)
                                        found_agent_ids.add(endpoint_id)
                                        log(f"✅ assignees에서 에이전트 찾음: {endpoint_id}")
        except Exception as e:
            log(f"⚠️ assignees 파싱 에러 (무시): {str(e)[:200]}...")
            handle_error("assignees파싱", e)

    return agent_list

# ============================================================================
# 피드백 작업 처리
# ============================================================================

async def process_feedback_task(row: Dict):
    """피드백 작업 처리"""
    todo_id = row['id']
    user_ids = row.get('user_id', '')
    assignees = row.get('assignees', None)
    feedback_raw = row.get('feedback', '')
    description = row.get('description', '')

    # 같은 워크아이템에 피드백이 여러 번 추가될 수 있으므로, 이전에 처리한 뒤 새로
    # 추가된 항목만 가져와 하나로 합친다 (최신 1건만 보고 나머지를 놓치지 않도록).
    collected_count = row.get('feedback_collected_count') or 0
    new_items = extract_new_feedback_items(feedback_raw, collected_count)
    feedback = '\n\n'.join(content for content, _uid, _time in new_items if content)

    if not feedback:
        log(f"⚠️ 새로 수집할 피드백 없음, 건너뜀: todo_id={todo_id}")
        await update_feedback_status(todo_id, 'FAILED')
        return

    try:
        await mark_feedback_collected_count(todo_id, collected_count + len(new_items))
        await update_feedback_status(todo_id, 'PROCESSING')
        log(f"피드백 작업 처리 시작: id={todo_id}")

        agents = await get_agents_info(user_ids, assignees)
        if not agents:
            log(f"⚠️ 에이전트 정보 없음: user_ids={user_ids}")
            try:
                await update_feedback_status(todo_id, 'FAILED')
            except Exception:
                pass
            return

        log(f"에이전트 {len(agents)}명 조회 완료")

        events = await fetch_events_by_todo_id(todo_id)
        log(f"이벤트 로그 조회 완료: todo_id={todo_id}, count={len(events)}")

        matching_result = await match_feedback_to_agents(
            feedback, agents, description, events,
        )
        agent_feedbacks = matching_result.get('agent_feedbacks', [])

        if not agent_feedbacks:
            log("⚠️ 매칭된 피드백 없음")
            try:
                await update_feedback_status(todo_id, 'COMPLETED')
            except Exception:
                pass
            return

        log(f"학습 후보 생성 완료: {len(agent_feedbacks)}개")

        had_any_error = False
        for feedback_item in agent_feedbacks:
            agent_id = feedback_item.get('agent_id')
            agent_name = feedback_item.get('agent_name', 'Unknown')
            learning_candidate = feedback_item.get('learning_candidate', {})

            if not learning_candidate:
                log(f"⚠️ 에이전트 {agent_name}의 학습 후보가 비어있음, 건너뜀")
                continue

            agent_info = _get_agent_by_id(agent_id)
            if not agent_info:
                log(f"⚠️ 에이전트 정보 없음: {agent_id}")
                continue

            feedback_content = learning_candidate.get('content', '')

            try:
                log(f"🤖 Deep Agent로 피드백 처리: {agent_name}")
                result = await process_feedback_with_deep_agent(
                    agent_id=agent_id,
                    agent_info=agent_info,
                    feedback_content=feedback_content,
                    task_description=description,
                    events=events,
                )
                if result.get("error"):
                    had_any_error = True
                    log(f"⚠️ 에이전트 {agent_name}: 에러 (계속 진행): {result.get('error')[:200]}...")
            except Exception as feedback_error:
                had_any_error = True
                log(f"⚠️ 에이전트 {agent_name} 피드백 처리 실패 (계속 진행): {str(feedback_error)[:200]}...")
                handle_error(f"피드백처리({agent_name})", feedback_error)
                continue

        log(f"피드백 작업 처리 완료: id={todo_id}")
        try:
            await update_feedback_status(todo_id, 'FAILED' if had_any_error else 'COMPLETED')
        except Exception as status_error:
            log(f"⚠️ 상태 업데이트 실패 (무시): {str(status_error)[:200]}...")

    except Exception as e:
        log(f"⚠️ 피드백 작업 처리 에러 (폴링 계속): {str(e)[:200]}...")
        handle_error("피드백작업처리", e)
        try:
            await update_feedback_status(todo_id, 'FAILED')
        except Exception:
            pass

