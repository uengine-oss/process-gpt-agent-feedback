"""scripts/backfill_skill_contributions.py 의 백필 로직(작성자 합집합, 승인자 중복 제거,
이미 기록된 항목 건너뛰기) 검증."""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_skill_contributions import backfill


def _make_supabase(proposals, existing_contributions):
    def table(name):
        m = MagicMock()
        if name == "feedback_proposals":
            m.select.return_value.execute.return_value = MagicMock(data=proposals)
        elif name == "skill_contributions":
            m.select.return_value.execute.return_value = MagicMock(data=existing_contributions)
            m.insert.return_value.execute.return_value = MagicMock(data=[{"id": "new"}])
        return m

    client = MagicMock()
    client.table.side_effect = table
    return client


@patch("scripts.backfill_skill_contributions.record_skill_contribution")
@patch("scripts.backfill_skill_contributions.get_db_client")
def test_backfill_records_authors_and_approver_once_each(mock_get_client, mock_record):
    proposal = {
        "id": "p1",
        "tenant_id": "t1",
        "collected_items": [
            {"todo_id": "todo1", "user_id": "author1"},
            {"todo_id": "todo2", "user_id": "author2"},
        ],
        "targets": [
            {"type": "SKILL", "status": "APPROVED", "name": "스킬A", "decided_by": "approver1", "decided_by_name": "승인자"},
        ],
    }
    mock_get_client.return_value = _make_supabase([proposal], [])

    result = backfill()

    assert result["recorded"] == 3
    recorded_ids = {c.kwargs["contributor_user_id"] for c in mock_record.call_args_list}
    assert recorded_ids == {"author1", "author2", "approver1"}
    assert all(c.kwargs["contribution_type"] == "PROPOSAL_APPROVED" for c in mock_record.call_args_list)


@patch("scripts.backfill_skill_contributions.record_skill_contribution")
@patch("scripts.backfill_skill_contributions.get_db_client")
def test_backfill_skips_pending_and_rejected_targets(mock_get_client, mock_record):
    proposal = {
        "id": "p1", "tenant_id": "t1",
        "collected_items": [{"todo_id": "todo1", "user_id": "author1"}],
        "targets": [
            {"type": "SKILL", "status": "PENDING", "name": "스킬A"},
            {"type": "SKILL", "status": "REJECTED", "name": "스킬B"},
            {"type": "DMN_RULE", "status": "APPROVED", "name": "규칙A"},
        ],
    }
    mock_get_client.return_value = _make_supabase([proposal], [])

    result = backfill()

    assert result["recorded"] == 0
    mock_record.assert_not_called()


@patch("scripts.backfill_skill_contributions.record_skill_contribution")
@patch("scripts.backfill_skill_contributions.get_db_client")
def test_backfill_is_idempotent_against_existing_rows(mock_get_client, mock_record):
    proposal = {
        "id": "p1", "tenant_id": "t1",
        "collected_items": [{"todo_id": "todo1", "user_id": "author1"}],
        "targets": [
            {"type": "SKILL", "status": "APPROVED", "name": "스킬A", "decided_by": "approver1"},
        ],
    }
    existing = [{"skill_name": "스킬A", "contributor_user_id": "author1", "source_proposal_id": "p1"}]
    mock_get_client.return_value = _make_supabase([proposal], existing)

    result = backfill()

    assert result["recorded"] == 1
    assert result["skipped_existing"] == 1
    recorded_ids = {c.kwargs["contributor_user_id"] for c in mock_record.call_args_list}
    assert recorded_ids == {"approver1"}
