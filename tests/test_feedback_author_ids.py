"""
피드백 작성자 배열 추출 테스트

대상 모듈:
- core.feedback_batch_manager._feedback_author_ids
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.feedback_batch_manager import _feedback_author_ids


class TestFeedbackAuthorIds:
    def test_single_author(self):
        items = [
            {"user_id": "u1", "time": "2026-07-01T00:00:00Z"},
            {"user_id": "u1", "time": "2026-07-02T00:00:00Z"},
        ]
        assert _feedback_author_ids(items) == ["u1"]

    def test_multiple_authors_ordered_by_first_contribution(self):
        items = [
            {"user_id": "u2", "time": "2026-07-02T00:00:00Z"},
            {"user_id": "u1", "time": "2026-07-01T00:00:00Z"},
            {"user_id": "u3", "time": "2026-07-03T00:00:00Z"},
        ]
        assert _feedback_author_ids(items) == ["u1", "u2", "u3"]

    def test_same_author_deduped_keeps_first_occurrence_position(self):
        items = [
            {"user_id": "u1", "time": "2026-07-01T00:00:00Z"},
            {"user_id": "u2", "time": "2026-07-02T00:00:00Z"},
            {"user_id": "u1", "time": "2026-07-03T00:00:00Z"},
        ]
        assert _feedback_author_ids(items) == ["u1", "u2"]

    def test_missing_or_blank_user_id_excluded(self):
        items = [
            {"time": "2026-07-01T00:00:00Z"},
            {"user_id": "", "time": "2026-07-02T00:00:00Z"},
            {"user_id": "   ", "time": "2026-07-03T00:00:00Z"},
            {"user_id": "u1", "time": "2026-07-04T00:00:00Z"},
        ]
        assert _feedback_author_ids(items) == ["u1"]

    def test_no_authors_returns_empty_list(self):
        items = [{"time": "2026-07-01T00:00:00Z"}, {"user_id": None}]
        assert _feedback_author_ids(items) == []

    def test_missing_time_sorts_first(self):
        items = [
            {"user_id": "u2", "time": "2026-07-01T00:00:00Z"},
            {"user_id": "u1"},
        ]
        assert _feedback_author_ids(items) == ["u1", "u2"]

    def test_empty_input(self):
        assert _feedback_author_ids([]) == []
