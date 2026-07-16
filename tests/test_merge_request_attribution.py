"""
병합 요청 requester/reviewer 귀속 테스트 (fix-merge-request-requester)

대상:
- core.feedback_batch_manager.apply_approved_dmn_target
- core.skill_api_client.update_skill_file
"""

import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.feedback_batch_manager import apply_approved_dmn_target
from core.skill_api_client import update_skill_file


def _batch_with_items(items):
    return {
        "id": "batch1",
        "tenant_id": "tenant1",
        "collected_items": items,
    }


_ARTIFACT = {
    "decision": {"name": "결정1", "description": "설명"},
    "rules": [{"when": "x", "then": "y"}],
}


class TestDmnMergeRequestAttribution:
    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.insert_dmn_merge_request")
    @patch("core.feedback_batch_manager.insert_draft_proc_def_version")
    @patch("core.feedback_batch_manager.dmn_decisions_rules_to_xml", return_value="<xml/>")
    @patch("core.feedback_batch_manager.compute_next_draft_version", return_value="1.0")
    @patch("core.feedback_batch_manager.merge_dmn_artifact_into_definition", return_value={})
    @patch("core.feedback_batch_manager._generate_dmn_proc_def_id", return_value="dmn_test")
    @patch("core.feedback_batch_manager.get_agents_info", new_callable=AsyncMock, return_value=[])
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock, return_value=[])
    async def test_multiple_authors_deduped_and_reviewer_separate(
        self,
        mock_fetch_rows,
        mock_get_agents,
        mock_gen_id,
        mock_merge,
        mock_next_version,
        mock_xml,
        mock_insert_version,
        mock_insert_pr,
    ):
        mock_insert_version.return_value = {"uuid": "v1"}
        mock_insert_pr.return_value = {"id": "pr1"}

        batch = _batch_with_items(
            [
                {"user_id": "author-a", "time": "2026-07-01T00:00:00Z"},
                {"user_id": "author-b", "time": "2026-07-02T00:00:00Z"},
                {"user_id": "author-a", "time": "2026-07-03T00:00:00Z"},
            ]
        )

        await apply_approved_dmn_target(batch, _ARTIFACT, approver_id="approver-x")

        mock_insert_pr.assert_called_once()
        _, kwargs = mock_insert_pr.call_args
        assert kwargs["requester_ids"] == ["author-a", "author-b"]
        assert kwargs["reviewer_id"] == "approver-x"
        assert "approver-x" not in kwargs["requester_ids"]

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.insert_dmn_merge_request")
    @patch("core.feedback_batch_manager.insert_draft_proc_def_version")
    @patch("core.feedback_batch_manager.dmn_decisions_rules_to_xml", return_value="<xml/>")
    @patch("core.feedback_batch_manager.compute_next_draft_version", return_value="1.0")
    @patch("core.feedback_batch_manager.merge_dmn_artifact_into_definition", return_value={})
    @patch("core.feedback_batch_manager._generate_dmn_proc_def_id", return_value="dmn_test")
    @patch("core.feedback_batch_manager.get_agents_info", new_callable=AsyncMock, return_value=[])
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock, return_value=[])
    async def test_approver_who_also_left_feedback_appears_in_both(
        self,
        mock_fetch_rows,
        mock_get_agents,
        mock_gen_id,
        mock_merge,
        mock_next_version,
        mock_xml,
        mock_insert_version,
        mock_insert_pr,
    ):
        mock_insert_version.return_value = {"uuid": "v1"}
        mock_insert_pr.return_value = {"id": "pr1"}

        batch = _batch_with_items(
            [
                {"user_id": "both-user", "time": "2026-07-01T00:00:00Z"},
                {"user_id": "other-user", "time": "2026-07-02T00:00:00Z"},
            ]
        )

        await apply_approved_dmn_target(batch, _ARTIFACT, approver_id="both-user")

        _, kwargs = mock_insert_pr.call_args
        assert kwargs["reviewer_id"] == "both-user"
        assert kwargs["requester_ids"].count("both-user") == 1
        assert "other-user" in kwargs["requester_ids"]


class TestSkillCommitRequestBody:
    @patch("core.skill_api_client._make_request")
    def test_requester_and_reviewer_included_when_present(self, mock_request):
        mock_request.return_value = {"committed": True}

        update_skill_file(
            "스킬1",
            "SKILL.md",
            "content",
            "tenant1",
            requester_ids=["author-a", "author-b"],
            reviewer_id="approver-x",
        )

        _, kwargs = mock_request.call_args
        body = kwargs["json_data"]
        assert body["requester_id"] == ["author-a", "author-b"]
        assert body["reviewer_id"] == "approver-x"

    @patch("core.skill_api_client._make_request")
    def test_requester_key_omitted_when_no_authors(self, mock_request):
        mock_request.return_value = {"committed": True}

        update_skill_file(
            "스킬1",
            "SKILL.md",
            "content",
            "tenant1",
            requester_ids=[],
            reviewer_id="approver-x",
        )

        _, kwargs = mock_request.call_args
        body = kwargs["json_data"]
        assert "requester_id" not in body
        assert body["reviewer_id"] == "approver-x"

    @patch("core.skill_api_client._make_request")
    def test_reviewer_key_omitted_when_absent(self, mock_request):
        mock_request.return_value = {"committed": True}

        update_skill_file(
            "스킬1",
            "SKILL.md",
            "content",
            "tenant1",
            requester_ids=["author-a"],
        )

        _, kwargs = mock_request.call_args
        body = kwargs["json_data"]
        assert body["requester_id"] == ["author-a"]
        assert "reviewer_id" not in body
