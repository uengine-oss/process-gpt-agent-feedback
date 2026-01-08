"""
에이전트 피드백 처리 전체 흐름 테스트

대상 모듈:
- core.polling_manager.process_feedback_task
- core.polling_manager.get_agents_info
- core.feedback_chain.execute_crud_operation
"""

import sys
import os
from typing import Dict, Any

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# 상위 디렉토리를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.polling_manager import get_agents_info, process_feedback_task
from core.feedback_chain import execute_crud_operation


# ============================================================================
# polling_manager 플로우 테스트
# ============================================================================


class TestPollingManagerFlow:
    """polling_manager 기반 에이전트 피드백 처리 플로우 테스트"""

    @pytest.mark.asyncio
    @patch("core.polling_manager._get_agent_by_id")
    async def test_get_agents_info_multiple_ids(self, mock_get_agent_by_id):
        """콤마로 구분된 user_id 목록으로 에이전트 정보를 조회하는지 테스트"""
        # given
        mock_get_agent_by_id.side_effect = [
            {"id": "agent-1", "name": "에이전트1"},
            None,
            {"id": "agent-2", "name": "에이전트2"},
        ]

        user_ids = "agent-1,  invalid ,agent-2"

        # when
        agents = await get_agents_info(user_ids)

        # then
        assert len(agents) == 2
        assert agents[0]["id"] == "agent-1"
        assert agents[1]["id"] == "agent-2"
        assert mock_get_agent_by_id.call_count == 3

    @pytest.mark.asyncio
    @patch("core.polling_manager.process_feedback_with_chain", new_callable=AsyncMock)
    @patch("core.polling_manager._get_agent_by_id")
    @patch("core.polling_manager.match_feedback_to_agents", new_callable=AsyncMock)
    @patch("core.polling_manager.get_agents_info", new_callable=AsyncMock)
    async def test_process_feedback_task_happy_path(
        self,
        mock_get_agents_info,
        mock_match_feedback_to_agents,
        mock_get_agent_by_id,
        mock_process_feedback_with_chain,
    ):
        """정상 플로우: 에이전트 조회 → 매칭 → 체인 호출까지 수행되는지 테스트"""
        # given
        row: Dict[str, Any] = {
            "id": "todo-1",
            "user_id": "agent-1",
            "feedback": "작업 결과가 요구사항과 다릅니다. 다음과 같이 수정해주세요...",
            "description": "리포트 작성 작업",
        }

        # 에이전트 정보 조회
        mock_get_agents_info.return_value = [
            {"id": "agent-1", "name": "에이전트1", "role": "writer", "goal": "좋은 리포트 작성"}
        ]

        # LLM 매칭 결과
        mock_match_feedback_to_agents.return_value = {
            "agent_feedbacks": [
                {
                    "agent_id": "agent-1",
                    "agent_name": "에이전트1",
                    "learning_candidate": {
                        "content": "리포트 작성 시 반드시 요약 섹션을 포함해야 합니다.",
                        "intent_hint": "지침/절차",
                    },
                }
            ]
        }

        # 개별 에이전트 정보 조회
        mock_get_agent_by_id.return_value = {
            "id": "agent-1",
            "name": "에이전트1",
            "role": "writer",
            "goal": "좋은 리포트 작성",
        }

        # when
        await process_feedback_task(row)

        # then
        mock_get_agents_info.assert_awaited_once_with("agent-1")
        mock_match_feedback_to_agents.assert_awaited_once()

        # process_feedback_with_chain이 올바른 파라미터로 호출되었는지 검증
        assert mock_process_feedback_with_chain.await_count == 1
        called_kwargs = mock_process_feedback_with_chain.call_args.kwargs

        assert called_kwargs["agent_id"] == "agent-1"
        assert called_kwargs["agent_info"]["id"] == "agent-1"
        assert called_kwargs["feedback_content"].startswith("리포트 작성 시 반드시")
        assert called_kwargs["task_description"] == "리포트 작성 작업"

    @pytest.mark.asyncio
    @patch("core.polling_manager.match_feedback_to_agents", new_callable=AsyncMock)
    @patch("core.polling_manager.get_agents_info", new_callable=AsyncMock)
    async def test_process_feedback_task_no_agents(
        self,
        mock_get_agents_info,
        mock_match_feedback_to_agents,
    ):
        """에이전트가 조회되지 않으면 이후 단계가 실행되지 않아야 함"""
        # given
        row = {
            "id": "todo-2",
            "user_id": "non-existent-agent",
            "feedback": "피드백 내용",
            "description": "작업 설명",
        }
        mock_get_agents_info.return_value = []

        # when
        await process_feedback_task(row)

        # then
        mock_get_agents_info.assert_awaited_once()
        mock_match_feedback_to_agents.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("core.polling_manager.process_feedback_with_chain", new_callable=AsyncMock)
    @patch("core.polling_manager._get_agent_by_id")
    @patch("core.polling_manager.match_feedback_to_agents", new_callable=AsyncMock)
    @patch("core.polling_manager.get_agents_info", new_callable=AsyncMock)
    async def test_process_feedback_task_no_matched_feedbacks(
        self,
        mock_get_agents_info,
        mock_match_feedback_to_agents,
        mock_get_agent_by_id,
        mock_process_feedback_with_chain,
    ):
        """매칭 결과에 agent_feedbacks가 없으면 체인 호출이 없어야 함"""
        # given
        row = {
            "id": "todo-3",
            "user_id": "agent-1",
            "feedback": "피드백 내용",
            "description": "작업 설명",
        }

        mock_get_agents_info.return_value = [
            {"id": "agent-1", "name": "에이전트1", "role": "writer", "goal": "테스트"}
        ]
        mock_match_feedback_to_agents.return_value = {"agent_feedbacks": []}

        # when
        await process_feedback_task(row)

        # then
        mock_get_agents_info.assert_awaited_once()
        mock_match_feedback_to_agents.assert_awaited_once()
        mock_process_feedback_with_chain.assert_not_awaited()


# ============================================================================
# feedback_chain.execute_crud_operation 테스트
# ============================================================================


class TestFeedbackChainCrudExecution:
    """feedback_chain.execute_crud_operation의 CRUD 라우팅 테스트"""

    @pytest.mark.asyncio
    @patch("core.feedback_chain.commit_to_skill", new_callable=AsyncMock)
    @patch("core.feedback_chain.commit_to_dmn_rule", new_callable=AsyncMock)
    @patch("core.feedback_chain.commit_to_memory", new_callable=AsyncMock)
    async def test_execute_crud_operation_ignore(
        self,
        mock_commit_to_memory,
        mock_commit_to_dmn_rule,
        mock_commit_to_skill,
    ):
        """operation이 IGNORE인 경우 어떤 커밋도 일어나지 않아야 함"""
        chain_result = {
            "target": "MEMORY",
            "artifacts": {"memory": "내용"},
            "conflict_analysis": {
                "operation": "IGNORE",
                "conflict_reason": "중복된 내용",
            },
        }

        await execute_crud_operation(
            agent_id="agent-1",
            chain_result=chain_result,
            existing_knowledge={},
            original_content="원본 피드백",
        )

        mock_commit_to_memory.assert_not_awaited()
        mock_commit_to_dmn_rule.assert_not_awaited()
        mock_commit_to_skill.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("core.feedback_chain.commit_to_memory", new_callable=AsyncMock)
    async def test_execute_crud_operation_memory_create(self, mock_commit_to_memory):
        """MEMORY 타겟 + CREATE 작업이 commit_to_memory로 위임되는지 테스트"""
        chain_result = {
            "target": "MEMORY",
            "artifacts": {"memory": "새로운 지침 내용"},
            "conflict_analysis": {
                "operation": "CREATE",
                "matched_item": None,
            },
        }

        await execute_crud_operation(
            agent_id="agent-1",
            chain_result=chain_result,
            existing_knowledge={},
            original_content="원본 피드백",
        )

        mock_commit_to_memory.assert_awaited_once()
        args, kwargs = mock_commit_to_memory.call_args

        assert args[0] == "agent-1"
        assert kwargs["content"] == "새로운 지침 내용"
        assert kwargs["source_type"] == "guideline"
        assert kwargs["operation"] == "CREATE"
        assert kwargs["memory_id"] is None

    @pytest.mark.asyncio
    @patch("core.feedback_chain.commit_to_dmn_rule", new_callable=AsyncMock)
    async def test_execute_crud_operation_dmn_update_with_matched_id(
        self,
        mock_commit_to_dmn_rule,
    ):
        """DMN_RULE 타겟 + UPDATE 작업에서 matched_item id가 rule_id로 전달되는지 테스트"""
        chain_result = {
            "target": "DMN_RULE",
            "artifacts": {
                "dmn": {
                    "name": "할인 규칙",
                    "condition": "amount >= 1000000",
                    "action": "승인 필요",
                }
            },
            "conflict_analysis": {
                "operation": "UPDATE",
                "matched_item": {
                    "id": "existing-rule-id",
                    "type": "DMN_RULE",
                },
            },
        }

        await execute_crud_operation(
            agent_id="agent-1",
            chain_result=chain_result,
            existing_knowledge={},
            original_content="원본 피드백",
        )

        mock_commit_to_dmn_rule.assert_awaited_once()
        args, kwargs = mock_commit_to_dmn_rule.call_args

        assert args[0] == "agent-1"
        assert kwargs["dmn_artifact"]["condition"] == "amount >= 1000000"
        assert kwargs["operation"] == "UPDATE"
        assert kwargs["rule_id"] == "existing-rule-id"

    @pytest.mark.asyncio
    @patch("core.feedback_chain.commit_to_memory", new_callable=AsyncMock)
    @patch("core.feedback_chain.commit_to_dmn_rule", new_callable=AsyncMock)
    async def test_execute_crud_operation_mixed_priority(
        self,
        mock_commit_to_dmn_rule,
        mock_commit_to_memory,
    ):
        """
        MIXED 타겟에서 DMN이 존재하면 MEMORY는 저장되지 않아야 함
        (우선순위: DMN_RULE > SKILL > MEMORY)
        """
        chain_result = {
            "target": "MIXED",
            "artifacts": {
                "dmn": {
                    "name": "나이 제한 규칙",
                    "condition": "age < 18",
                    "action": "20% 할인",
                },
                "memory": "일반적으로 미성년자는 할인 혜택을 준다.",
            },
            "conflict_analysis": {
                "operation": "CREATE",
                "matched_item": None,
            },
        }

        await execute_crud_operation(
            agent_id="agent-1",
            chain_result=chain_result,
            existing_knowledge={},
            original_content="원본 피드백",
        )

        # DMN_RULE은 호출되어야 함
        mock_commit_to_dmn_rule.assert_awaited_once()
        # MIXED에서 DMN이 있으면 MEMORY는 저장하지 않아야 함
        mock_commit_to_memory.assert_not_awaited()



