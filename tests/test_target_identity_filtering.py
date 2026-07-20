"""
"생성 없음, 기존 리소스 개선만" 정책 테스트 — 제안(target) 단계에서 PASS 판정이나
삭제된 원본 리소스를 걸러내는지 검증한다.

대상 모듈:
- core.feedback_batch_manager._fill_target_identity
- core.feedback_batch_manager._process_triggered_batch
"""

import sys
import os
import pytest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.feedback_batch_manager import _fill_target_identity, _process_triggered_batch


def _batch(**overrides):
    base = {
        "id": "batch1",
        "tenant_id": "tenant1",
        "proc_def_id": "proc1",
        "activity_id": "activity1",
        "collected_items": [{"todo_id": "todo1", "user_id": "author-a", "time": "2026-07-01T00:00:00Z"}],
    }
    base.update(overrides)
    return base


class TestFillTargetIdentityDropsCreate:
    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.fetch_proc_def_name", return_value=None)
    async def test_process_definition_dropped_when_source_proc_def_missing(self, mock_fetch_name):
        target = {"type": "PROCESS_DEFINITION", "artifact": {"summary": "변경"}}

        kept = await _fill_target_identity(_batch(), target)

        assert kept is False

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.fetch_proc_def_name", return_value="검토 프로세스")
    async def test_process_definition_kept_when_source_proc_def_exists(self, mock_fetch_name):
        target = {"type": "PROCESS_DEFINITION", "artifact": {"summary": "변경"}}

        kept = await _fill_target_identity(_batch(), target)

        assert kept is True
        assert target["id"] == "proc1"
        assert target["name"] == "검토 프로세스"

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.resolve_skill_identity", new_callable=AsyncMock, return_value={"decision": "PASS", "name": ""})
    @patch("core.feedback_batch_manager._representative_agent", new_callable=AsyncMock, return_value=None)
    @patch("core.feedback_batch_manager.load_activity_skills", return_value=["기존-스킬"])
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock, return_value=[])
    async def test_skill_dropped_when_no_matching_existing_skill(
        self, mock_rows, mock_activity_skills, mock_rep_agent, mock_resolve
    ):
        target = {"type": "SKILL", "artifact": "겹치지 않는 새 절차"}

        kept = await _fill_target_identity(_batch(), target)

        assert kept is False

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.resolve_skill_identity", new_callable=AsyncMock, return_value={"decision": "UPDATE", "name": "기존-스킬"})
    @patch("core.feedback_batch_manager._representative_agent", new_callable=AsyncMock, return_value=None)
    @patch("core.feedback_batch_manager.load_activity_skills", return_value=["기존-스킬"])
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock, return_value=[])
    async def test_skill_kept_when_matching_existing_skill(
        self, mock_rows, mock_activity_skills, mock_rep_agent, mock_resolve
    ):
        target = {"type": "SKILL", "artifact": "기존 절차 보완"}

        kept = await _fill_target_identity(_batch(), target)

        assert kept is True
        assert target["id"] == target["name"] == target["skill_name"] == "기존-스킬"

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager._representative_agent", new_callable=AsyncMock, return_value=None)
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock, return_value=[])
    async def test_dmn_dropped_when_no_representative_agent(self, mock_rows, mock_rep_agent):
        """DMN은 agent_id로만 후보를 조회하므로, 담당 에이전트가 없으면 비교할 기존
        리소스가 애초에 없다 — PASS 외의 결과가 나올 수 없으므로 즉시 드롭한다."""
        target = {"type": "DMN_RULE", "artifact": {"decision": {"name": "결정1"}, "rules": []}}

        kept = await _fill_target_identity(_batch(), target)

        assert kept is False

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.resolve_dmn_identity", new_callable=AsyncMock, return_value={"decision": "PASS", "id": None, "name": "결정1"})
    @patch("core.feedback_batch_manager.list_agent_dmn_rules", return_value=[])
    @patch("core.feedback_batch_manager._representative_agent", new_callable=AsyncMock, return_value={"id": "agent-1", "name": "에이전트1"})
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock, return_value=[])
    async def test_dmn_dropped_when_no_matching_existing_dmn(
        self, mock_rows, mock_rep_agent, mock_candidates, mock_resolve
    ):
        target = {"type": "DMN_RULE", "artifact": {"decision": {"name": "결정1"}, "rules": []}}

        kept = await _fill_target_identity(_batch(), target)

        assert kept is False

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.resolve_dmn_identity", new_callable=AsyncMock, return_value={"decision": "UPDATE", "id": "dmn_existing", "name": "결정1"})
    @patch("core.feedback_batch_manager.list_agent_dmn_rules", return_value=[{"id": "dmn_existing", "name": "결정1"}])
    @patch("core.feedback_batch_manager._representative_agent", new_callable=AsyncMock, return_value={"id": "agent-1", "name": "에이전트1"})
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock, return_value=[])
    async def test_dmn_kept_when_matching_existing_dmn(
        self, mock_rows, mock_rep_agent, mock_candidates, mock_resolve
    ):
        target = {"type": "DMN_RULE", "artifact": {"decision": {"name": "결정1"}, "rules": []}}

        kept = await _fill_target_identity(_batch(), target)

        assert kept is True
        assert target["id"] == "dmn_existing"


class TestProcessTriggeredBatchDiscardsWhenNothingSurvives:
    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.update_feedback_status", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager.mark_batch_discarded", new_callable=AsyncMock, return_value=True)
    @patch("core.feedback_batch_manager.mark_batch_proposed", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager._fill_target_identity", new_callable=AsyncMock, return_value=False)
    @patch(
        "core.feedback_batch_manager.classify_and_extract_proposal",
        new_callable=AsyncMock,
        return_value=[{"type": "DMN_RULE", "artifact": {"decision": {"name": "결정1"}, "rules": []}}],
    )
    async def test_batch_discarded_when_all_targets_filtered_out(
        self, mock_classify, mock_fill_identity, mock_proposed, mock_discarded, mock_status
    ):
        await _process_triggered_batch(_batch())

        mock_proposed.assert_not_called()
        mock_discarded.assert_called_once()
        mock_status.assert_called_once_with("todo1", "REJECTED")

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.mark_batch_discarded", new_callable=AsyncMock, return_value=True)
    @patch("core.feedback_batch_manager.mark_batch_proposed", new_callable=AsyncMock, return_value=True)
    @patch("core.feedback_batch_manager._fill_target_identity", new_callable=AsyncMock)
    @patch(
        "core.feedback_batch_manager.classify_and_extract_proposal",
        new_callable=AsyncMock,
        return_value=[
            {"type": "SKILL", "artifact": "규칙"},
            {"type": "DMN_RULE", "artifact": {"decision": {"name": "결정1"}, "rules": []}},
        ],
    )
    async def test_only_surviving_targets_are_proposed(
        self, mock_classify, mock_fill_identity, mock_proposed, mock_discarded
    ):
        # SKILL은 살아남고 DMN_RULE은 드롭된다
        mock_fill_identity.side_effect = [True, False]

        await _process_triggered_batch(_batch())

        mock_discarded.assert_not_called()
        mock_proposed.assert_called_once()
        _, kept_targets, _ = mock_proposed.call_args[0]
        assert [t["type"] for t in kept_targets] == ["SKILL"]
