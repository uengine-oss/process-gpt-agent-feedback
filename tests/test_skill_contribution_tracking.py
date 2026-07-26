"""
스킬 기여 이력 추적(스펙 agent-feedback_skill-contribution-tracking) 테스트.

commit_to_skill 의 기여자 기록 훅, GET /api/skills/{id}/contributors 엔드포인트,
apply_approved_proposal 의 기여자(원 피드백 작성자+승인자) 계산을 검증한다.
"""

import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.learning_committers.skill_committer import commit_to_skill


SKILL_ARTIFACT = {
    "name": "테스트 스킬",
    "description": "설명",
    "body_markdown": "# 테스트 스킬\n\n본문",
}


# ----------------------------------------------------------------- commit_to_skill 훅


class TestSkillContributionRecording:
    """CREATE는 update-existing-only 정책(refactor: restrict feedback pipeline to
    update-existing-only)으로 완전히 폐기됐으므로, 기여 기록은 UPDATE 경로로만 검증한다."""

    @pytest.mark.asyncio
    @patch("core.learning_committers.skill_committer.record_skill_contribution")
    @patch("core.learning_committers.skill_committer._get_agent_by_id")
    @patch("core.learning_committers.skill_committer.update_skill_file")
    @patch("core.learning_committers.skill_committer.check_skill_exists")
    async def test_update_records_contribution_for_each_contributor(
        self, mock_check_exists, mock_update_file, mock_get_agent, mock_record
    ):
        mock_get_agent.return_value = {"id": "test_agent", "tenant_id": "test_tenant"}
        mock_check_exists.return_value = True
        mock_update_file.return_value = {"message": "Success"}

        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=SKILL_ARTIFACT,
            operation="UPDATE",
            skill_id="테스트 스킬",
            contributor_user_ids=["u1", "u2"],
        )

        assert mock_record.call_count == 2
        recorded_types = {c.kwargs["contribution_type"] for c in mock_record.call_args_list}
        assert recorded_types == {"MODIFIED"}
        recorded_users = {c.kwargs["contributor_user_id"] for c in mock_record.call_args_list}
        assert recorded_users == {"u1", "u2"}
        assert all(c.kwargs["skill_name"] == "테스트 스킬" for c in mock_record.call_args_list)
        assert all(c.kwargs["tenant_id"] == "test_tenant" for c in mock_record.call_args_list)

    @pytest.mark.asyncio
    @patch("core.learning_committers.skill_committer.record_skill_contribution")
    @patch("core.learning_committers.skill_committer._get_agent_by_id")
    @patch("core.learning_committers.skill_committer.update_skill_file")
    @patch("core.learning_committers.skill_committer.check_skill_exists")
    async def test_update_records_modified_contribution(
        self, mock_check_exists, mock_update_file, mock_get_agent, mock_record
    ):
        mock_get_agent.return_value = {"id": "test_agent", "tenant_id": "test_tenant"}
        mock_check_exists.return_value = True
        mock_update_file.return_value = {"message": "Success"}

        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=SKILL_ARTIFACT,
            operation="UPDATE",
            skill_id="테스트 스킬",
            contributor_user_ids=["u1"],
        )

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["contribution_type"] == "MODIFIED"
        assert mock_record.call_args.kwargs["contributor_user_id"] == "u1"

    @pytest.mark.asyncio
    @patch("core.learning_committers.skill_committer.record_skill_contribution")
    @patch("core.learning_committers.skill_committer._get_agent_by_id")
    @patch("core.learning_committers.skill_committer.update_skill_file")
    @patch("core.learning_committers.skill_committer.check_skill_exists")
    async def test_proposal_approval_records_proposal_approved_type(
        self, mock_check_exists, mock_update_file, mock_get_agent, mock_record
    ):
        mock_get_agent.return_value = {"id": "test_agent", "tenant_id": "test_tenant"}
        mock_check_exists.return_value = True
        mock_update_file.return_value = {"message": "Success"}

        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=SKILL_ARTIFACT,
            operation="UPDATE",
            skill_id="테스트 스킬",
            contributor_user_ids=["author1", "approver1"],
            contribution_source="proposal_approval",
        )

        recorded_types = {c.kwargs["contribution_type"] for c in mock_record.call_args_list}
        assert recorded_types == {"PROPOSAL_APPROVED"}
        recorded_users = {c.kwargs["contributor_user_id"] for c in mock_record.call_args_list}
        assert recorded_users == {"author1", "approver1"}

    @pytest.mark.asyncio
    @patch("core.learning_committers.skill_committer.record_skill_contribution")
    @patch("core.learning_committers.skill_committer._get_agent_by_id")
    @patch("core.learning_committers.skill_committer.update_skill_file")
    @patch("core.learning_committers.skill_committer.check_skill_exists")
    async def test_no_contributor_ids_skips_recording(
        self, mock_check_exists, mock_update_file, mock_get_agent, mock_record
    ):
        mock_get_agent.return_value = {"id": "test_agent", "tenant_id": "test_tenant"}
        mock_check_exists.return_value = True
        mock_update_file.return_value = {"message": "Success"}

        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=SKILL_ARTIFACT,
            operation="UPDATE",
            skill_id="테스트 스킬",
        )

        mock_record.assert_not_called()

    @pytest.mark.asyncio
    @patch("core.learning_committers.skill_committer.record_skill_contribution")
    @patch("core.learning_committers.skill_committer._get_agent_by_id")
    @patch("core.learning_committers.skill_committer.update_skill_file")
    @patch("core.learning_committers.skill_committer.check_skill_exists")
    async def test_duplicate_contributor_ids_recorded_once(
        self, mock_check_exists, mock_update_file, mock_get_agent, mock_record
    ):
        mock_get_agent.return_value = {"id": "test_agent", "tenant_id": "test_tenant"}
        mock_check_exists.return_value = True
        mock_update_file.return_value = {"message": "Success"}

        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=SKILL_ARTIFACT,
            operation="UPDATE",
            skill_id="테스트 스킬",
            contributor_user_ids=["u1", "u1", "u2"],
        )

        assert mock_record.call_count == 2


# ----------------------------------------------------------------- GET /api/skills/{id}/contributors


class TestSkillContributorRoute:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core.skill_contributor_routes import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @patch("core.skill_contributor_routes.fetch_skill_contributors")
    @patch("core.skill_contributor_routes.check_skill_exists")
    def test_computes_relative_share(self, mock_exists, mock_fetch):
        mock_exists.return_value = True
        mock_fetch.return_value = [
            {"contributor_user_id": "u1", "contributor_name": "앨리스", "contribution_type": "CREATED"},
            {"contributor_user_id": "u1", "contributor_name": "앨리스", "contribution_type": "MODIFIED"},
            {"contributor_user_id": "u2", "contributor_name": "밥", "contribution_type": "PROPOSAL_APPROVED"},
        ]

        resp = self._client().get("/api/skills/테스트 스킬/contributors", params={"tenant_id": "t1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_contributions"] == 3
        by_id = {c["contributor_user_id"]: c for c in body["contributors"]}
        assert by_id["u1"]["count"] == 2
        assert by_id["u1"]["share"] == pytest.approx(2 / 3, abs=1e-4)
        assert by_id["u2"]["count"] == 1
        assert by_id["u2"]["share"] == pytest.approx(1 / 3, abs=1e-4)

    @patch("core.skill_contributor_routes.fetch_skill_contributors")
    @patch("core.skill_contributor_routes.check_skill_exists")
    def test_no_contributions_returns_empty_list(self, mock_exists, mock_fetch):
        mock_exists.return_value = True
        mock_fetch.return_value = []

        resp = self._client().get("/api/skills/외로운 스킬/contributors", params={"tenant_id": "t1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["contributors"] == []
        assert body["total_contributions"] == 0

    @patch("core.skill_contributor_routes.check_skill_exists")
    def test_unknown_skill_returns_404(self, mock_exists):
        mock_exists.return_value = False

        resp = self._client().get("/api/skills/없는 스킬/contributors", params={"tenant_id": "t1"})
        assert resp.status_code == 404


# ----------------------------------------------------------------- apply_approved_proposal 기여자 계산


class TestApplyApprovedProposalContributors:
    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.process_feedback_with_deep_agent", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager.update_feedback_status", new_callable=AsyncMock)
    @patch("core.database.fetch_events_by_todo_id", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager.get_agents_info", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock)
    async def test_contributor_ids_union_feedback_authors_and_approver(
        self, mock_fetch_rows, mock_get_agents, mock_fetch_events, mock_update_status, mock_process,
    ):
        from core.feedback_batch_manager import apply_approved_proposal

        mock_fetch_rows.return_value = [
            {"id": "todo1", "user_id": "author1", "assignees": "[]", "description": "설명", "end_date": None, "updated_at": None},
            {"id": "todo2", "user_id": "author2", "assignees": "[]", "description": "설명", "end_date": None, "updated_at": None},
        ]
        mock_get_agents.return_value = []  # 활동 전용 경로로 유도(단순화)
        mock_fetch_events.return_value = []
        mock_process.return_value = {"error": None}

        batch = {
            "id": "batch1",
            "tenant_id": "t1",
            "proc_def_id": "pd1",
            "activity_id": "a1",
            "collected_items": [
                {"todo_id": "todo1", "content": "피드백1", "time": "2026-01-01T00:00:00Z", "user_id": "author1"},
                {"todo_id": "todo2", "content": "피드백2", "time": "2026-01-02T00:00:00Z", "user_id": "author2"},
            ],
        }

        await apply_approved_proposal(
            batch, extracted_rule="규칙", approver_id="approver1", approver_name="승인자",
        )

        mock_process.assert_called_once()
        contributor_ids = mock_process.call_args.kwargs["contributor_user_ids"]
        assert set(contributor_ids) == {"author1", "author2", "approver1"}
        assert mock_process.call_args.kwargs["contribution_source"] == "proposal_approval"

    @pytest.mark.asyncio
    @patch("core.feedback_batch_manager.process_feedback_with_deep_agent", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager.update_feedback_status", new_callable=AsyncMock)
    @patch("core.database.fetch_events_by_todo_id", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager.get_agents_info", new_callable=AsyncMock)
    @patch("core.feedback_batch_manager.fetch_todolist_rows_by_ids", new_callable=AsyncMock)
    async def test_no_approver_id_still_includes_authors(
        self, mock_fetch_rows, mock_get_agents, mock_fetch_events, mock_update_status, mock_process,
    ):
        from core.feedback_batch_manager import apply_approved_proposal

        mock_fetch_rows.return_value = [
            {"id": "todo1", "user_id": "author1", "assignees": "[]", "description": "설명", "end_date": None, "updated_at": None},
        ]
        mock_get_agents.return_value = []
        mock_fetch_events.return_value = []
        mock_process.return_value = {"error": None}

        batch = {
            "id": "batch1", "tenant_id": "t1", "proc_def_id": "pd1", "activity_id": "a1",
            "collected_items": [
                {"todo_id": "todo1", "content": "피드백1", "time": "2026-01-01T00:00:00Z", "user_id": "author1"},
            ],
        }

        await apply_approved_proposal(batch, extracted_rule="규칙")

        contributor_ids = mock_process.call_args.kwargs["contributor_user_ids"]
        assert set(contributor_ids) == {"author1"}
