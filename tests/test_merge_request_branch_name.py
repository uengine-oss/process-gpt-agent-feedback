"""
resource_pull_requests.branch_name에 draft proc_def_version.version 값이
그대로 들어가는지 확인하는 테스트

대상 모듈:
- core.database.insert_dmn_merge_request
- core.database.insert_bpmn_merge_request
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import insert_dmn_merge_request, insert_bpmn_merge_request


def _mock_supabase(inserted_row):
    supabase = MagicMock()
    supabase.table.return_value.insert.return_value.execute.return_value.data = [inserted_row]
    return supabase


class TestMergeRequestBranchName:
    @patch("core.database.get_db_client")
    def test_dmn_merge_request_branch_name_uses_draft_version(self, mock_get_client):
        mock_get_client.return_value = _mock_supabase({"id": "pr1"})

        insert_dmn_merge_request(
            tenant_id="tenant1",
            proc_def_id="dmn1",
            version="4.0-fonqzvu4zbm",
            title="title",
            description="description",
        )

        supabase = mock_get_client.return_value
        inserted_row = supabase.table.return_value.insert.call_args[0][0]
        assert inserted_row["branch_name"] == "4.0-fonqzvu4zbm"

    @patch("core.database.get_db_client")
    def test_bpmn_merge_request_branch_name_uses_draft_version(self, mock_get_client):
        mock_get_client.return_value = _mock_supabase({"id": "pr1"})

        insert_bpmn_merge_request(
            tenant_id="tenant1",
            proc_def_id="proc1",
            version="1.0-abcdefghijk",
            title="title",
            description="description",
        )

        supabase = mock_get_client.return_value
        inserted_row = supabase.table.return_value.insert.call_args[0][0]
        assert inserted_row["branch_name"] == "1.0-abcdefghijk"
