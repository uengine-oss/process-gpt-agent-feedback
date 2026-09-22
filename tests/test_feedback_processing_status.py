"""
피드백 처리 현황(내 피드백) 상태 판정과 target 적용 결과 기록 테스트.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.feedback_batch_manager import _definition_apply_outcome, sync_batch_feedback_status
from core.feedback_proposal_routes import _find_batch_for_item, _pending_stage, _round_stage, _target_stage


def _batch(status, targets=None, items=None):
    return {"id": "b1", "status": status, "targets": targets or [], "collected_items": items or []}


class TestTargetStage:
    def test_pending_and_rejected(self):
        assert _target_stage({"status": "PENDING"}, {}) == "PENDING_REVIEW"
        assert _target_stage({"status": "REJECTED"}, {}) == "REJECTED"

    def test_applied_becomes_merged_when_its_pull_request_is_merged(self):
        target = {"status": "APPROVED", "apply_status": "APPLIED", "apply_results": [{"pull_request_id": "pr1"}]}
        assert _target_stage(target, {"pr1": {"status": "OPEN"}}) == "APPLIED"
        assert _target_stage(target, {"pr1": {"status": "MERGED"}}) == "MERGED"

    def test_approved_without_apply_record_is_plain_approved(self):
        # 적용 기록이 생기기 전(이전 버전)에 승인된 target
        assert _target_stage({"status": "APPROVED"}, {}) == "APPROVED"

    def test_failed_and_no_change(self):
        assert _target_stage({"status": "APPROVED", "apply_status": "FAILED"}, {}) == "APPLY_FAILED"
        assert _target_stage({"status": "APPROVED", "apply_status": "NO_CHANGE"}, {}) == "NO_CHANGE"


class TestRoundStage:
    def test_collecting_classifying_and_discarded(self):
        assert _round_stage(_batch("COLLECTING"), []) == "COLLECTING"
        assert _round_stage(_batch("CLASSIFYING"), []) == "CLASSIFYING"
        assert _round_stage(_batch("DISCARDED"), []) == "DISCARDED"

    def test_proposed_without_targets_is_shown_as_discarded(self):
        # 분류 결과 없이 PROPOSED로 남은 이전 방식의 제안 — 영원히 "승인 대기"로 보이면 안 된다
        assert _round_stage(_batch("PROPOSED"), []) == "DISCARDED"

    def test_furthest_target_wins(self):
        targets = [{"stage": "REJECTED"}, {"stage": "APPLIED"}, {"stage": "PENDING_REVIEW"}]
        assert _round_stage(_batch("PROPOSED", targets), targets) == "APPLIED"


class TestPendingStage:
    def test_pending_feedback_waits_for_the_workitem_to_finish(self):
        assert _pending_stage({"proc_def_id": "p", "status": "IN_PROGRESS"}) == "WAITING_DONE"
        assert _pending_stage({"proc_def_id": "p", "status": "DONE"}) == "WAITING"
        assert _pending_stage({"proc_def_id": "", "status": "DONE"}) == "NOT_ELIGIBLE"


class TestMyFeedbackGroupsByWorkitem:
    @staticmethod
    async def _call(rows_mine, rows_participating=(), rows_assigned=(), batches=(), scope="participating"):
        from core import feedback_proposal_routes as r
        with patch.object(r, "fetch_todolist_rows_with_feedback_by_user", return_value=list(rows_mine)), \
             patch.object(r, "fetch_participating_proc_inst_ids", return_value=["i1"]), \
             patch.object(r, "fetch_feedback_rows_by_proc_inst_ids", return_value=list(rows_participating)), \
             patch.object(r, "fetch_feedback_rows_assigned_to", return_value=list(rows_assigned)), \
             patch.object(r, "fetch_batches_for_proc_defs", return_value=list(batches)), \
             patch.object(r, "fetch_resource_pull_requests_by_ids", return_value={}), \
             patch.object(r, "fetch_proc_def_names", return_value={"p": "할인 프로세스"}), \
             patch.object(r, "fetch_proc_inst_summaries", return_value={"i1": {"name": "주문 #1001", "status": "RUNNING"}}), \
             patch.object(r, "fetch_user_briefs", return_value={"u": {"name": "홍길동", "profile": "/p/u.png"}, "v": {"name": "김코드", "profile": None}}):
            return await r.list_my_feedback("tn", "u", scope=scope)

    @pytest.mark.asyncio
    async def test_feedback_is_grouped_into_rounds_per_workitem(self):
        row = {
            "id": "t1", "proc_def_id": "p", "activity_id": "a", "activity_name": "할인율 적용", "status": "DONE",
            "proc_inst_id": "i1", "feedback_collected_count": 3,
            "feedback": [
                {"time": "1", "content": "첫 회차 A", "user_id": "u"},
                {"time": "2", "content": "첫 회차 B", "user_id": "v"},
                {"time": "3", "content": "둘째 회차", "user_id": "u"},
                {"time": "4", "content": "아직 수집 전", "user_id": "u"},
            ],
        }
        first = {"id": "b1", "status": "PROPOSED", "first_collected_at": "2026-09-01T00:00:00+00:00",
                 "targets": [{"type": "SKILL", "status": "PENDING"}],
                 "collected_items": [{"todo_id": "t1", "user_id": "u", "time": "1"}, {"todo_id": "t1", "user_id": "v", "time": "2"}]}
        second = {"id": "b2", "status": "COLLECTING", "first_collected_at": "2026-09-02T00:00:00+00:00", "targets": [],
                  "collected_items": [{"todo_id": "t1", "user_id": "u", "time": "3"}]}
        result = await self._call([row], batches=[second, first])

        assert result["total"] == 1
        w = result["workitems"][0]
        assert (w["feedback_count"], w["my_feedback_count"], w["author_count"]) == (4, 3, 2)
        assert (w["proc_inst_name"], w["proc_inst_status"]) == ("주문 #1001", "RUNNING")
        assert [(x["round"], x["stage"], [f["content"] for f in x["feedbacks"]]) for x in w["rounds"]] == [
            (1, "PENDING_REVIEW", ["첫 회차 A", "첫 회차 B"]),
            (2, "COLLECTING", ["둘째 회차"]),
        ]
        other = w["rounds"][0]["feedbacks"][1]
        assert (other["author_name"], other["author_profile"], other["is_mine"]) == ("김코드", None, False)
        assert w["pending"]["stage"] == "WAITING"
        assert [f["content"] for f in w["pending"]["feedbacks"]] == ["아직 수집 전"]
        assert w["stage"] == "COLLECTING"

    @pytest.mark.asyncio
    async def test_participating_scope_includes_workitems_i_did_not_comment_on(self):
        mine = {"id": "t1", "proc_def_id": "p", "status": "DONE", "feedback": [{"time": "1", "content": "a", "user_id": "u"}]}
        others = {"id": "t2", "proc_def_id": "p", "status": "DONE", "proc_inst_id": "i1",
                  "feedback": [{"time": "2", "content": "b", "user_id": "v"}]}
        assigned = {"id": "t3", "proc_def_id": "p", "status": "DONE", "feedback": [{"time": "3", "content": "c", "user_id": "v"}]}
        result = await self._call([mine], rows_participating=[others, mine], rows_assigned=[assigned])
        assert sorted(w["todo_id"] for w in result["workitems"]) == ["t1", "t2", "t3"]

        result = await self._call([mine], rows_participating=[others], rows_assigned=[assigned], scope="mine")
        assert [w["todo_id"] for w in result["workitems"]] == ["t1"]


class TestFindBatchForItem:
    def test_matches_by_time_then_by_content_for_legacy_items(self):
        batch = _batch("COLLECTING", items=[
            {"todo_id": "t1", "user_id": "u1", "time": "2026-09-22T00:00:00Z", "content": "A"},
            {"todo_id": "t1", "user_id": "u1", "time": "", "content": "old"},
        ])
        index = {"t1": [batch]}
        assert _find_batch_for_item(index, "t1", {"time": "2026-09-22T00:00:00Z", "content": "A", "user_id": "u1"}) is batch
        assert _find_batch_for_item(index, "t1", {"time": "", "content": "old", "user_id": "u1"}) is batch
        assert _find_batch_for_item(index, "t1", {"time": "2026-09-22T00:00:00Z", "content": "A", "user_id": "someone"}) is None


class TestDefinitionApplyOutcome:
    def test_applied_when_any_draft_and_merge_request_created(self):
        outcome = _definition_apply_outcome("dmn", [
            {"applied": True, "dmn_id": "d1", "draft_version": "1.1", "resource_pull_request_id": "pr1"},
            {"applied": False, "error": "dmn_not_found"},
        ])
        assert outcome["apply_status"] == "APPLIED"
        assert outcome["apply_results"][0]["pull_request_id"] == "pr1"
        assert outcome["apply_results"][1]["error"] == "dmn_not_found"

    def test_failed_and_no_change(self):
        assert _definition_apply_outcome("bpmn", [{"applied": False, "error": "x"}])["apply_status"] == "FAILED"
        assert _definition_apply_outcome("dmn", [])["apply_status"] == "NO_CHANGE"


class TestSyncBatchFeedbackStatus:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("targets,expected", [
        ([{"status": "APPROVED", "apply_status": "APPLIED"}, {"status": "PENDING"}], "COMPLETED"),
        ([{"status": "REJECTED"}, {"status": "REJECTED"}], "REJECTED"),
        ([{"status": "APPROVED", "apply_status": "FAILED"}, {"status": "REJECTED"}], "FAILED"),
        ([{"status": "APPROVED", "apply_status": "NO_CHANGE"}], "COMPLETED"),
    ])
    async def test_final_status(self, targets, expected):
        batch = _batch("RESOLVED", targets, items=[{"todo_id": "t1"}, {"todo_id": "t2"}])
        with patch("core.feedback_batch_manager.update_feedback_status", new=AsyncMock()) as mock_update:
            await sync_batch_feedback_status(batch)
        assert [c.args for c in mock_update.call_args_list] == [("t1", expected), ("t2", expected)]

    @pytest.mark.asyncio
    async def test_leaves_status_alone_while_something_is_still_open(self):
        batch = _batch("PROPOSED", [{"status": "PENDING"}, {"status": "APPROVED", "apply_status": "APPLYING"}],
                       items=[{"todo_id": "t1"}])
        with patch("core.feedback_batch_manager.update_feedback_status", new=AsyncMock()) as mock_update:
            await sync_batch_feedback_status(batch)
        mock_update.assert_not_called()


class TestNoiseFeedback:
    @pytest.mark.parametrize("text", ["잘했어요", "다시 해줘", "한 번 더", "고마워요", "좋네요", "재시도", "감사합니다!", "ok"])
    def test_praise_and_bare_retries_are_noise(self, text):
        from core.feedback_processor import is_noise_feedback
        assert is_noise_feedback(text)

    @pytest.mark.parametrize("text", ["다시 해줘. 이번엔 표로", "VIP 고객은 20% 할인이 맞습니다", "좋은데 결론이 빠졌어요"])
    def test_feedback_with_content_is_kept(self, text):
        from core.feedback_processor import is_noise_feedback
        assert not is_noise_feedback(text)

    @pytest.mark.asyncio
    async def test_batch_of_only_noise_is_discarded_without_classification(self):
        from core import feedback_batch_manager as m
        batch = {"id": "b1", "collected_items": [{"todo_id": "t1", "content": "다시 해줘"}, {"todo_id": "t2", "content": "잘했어요"}]}
        with patch.object(m, "classify_and_extract_proposal", new=AsyncMock()) as classify, \
             patch.object(m, "mark_batch_discarded", new=AsyncMock(return_value=True)) as discard, \
             patch.object(m, "update_feedback_status", new=AsyncMock()):
            await m._process_triggered_batch(batch)
        classify.assert_not_called()
        assert discard.call_args.kwargs["reason"] == m.DISCARD_REASON_NO_CONTENT


class TestWorkitemUnit:
    def test_trigger_counts_workitems_not_feedback_messages(self):
        from core.feedback_batch_manager import is_batch_triggered
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        one_workitem_five_messages = [{"todo_id": "t1", "content": f"피드백 {i}"} for i in range(5)]
        assert not is_batch_triggered(one_workitem_five_messages, now)
        five_workitems = [{"todo_id": f"t{i}", "content": "피드백"} for i in range(5)]
        assert is_batch_triggered(five_workitems, now)

    @pytest.mark.asyncio
    async def test_all_new_feedback_of_a_workitem_is_appended_in_one_call(self):
        from core import feedback_batch_manager as m
        row = {
            "id": "t1", "tenant_id": "tn", "proc_def_id": "p", "activity_id": "a", "feedback_collected_count": 1,
            "feedback": [
                {"time": "2026-09-22T00:00:00Z", "content": "이미 처리됨", "user_id": "u"},
                {"time": "2026-09-22T01:00:00Z", "content": "새 피드백 1", "user_id": "u"},
                {"time": "2026-09-22T02:00:00Z", "content": "새 피드백 2", "user_id": "u"},
            ],
        }
        with patch.object(m, "append_workitem_feedback_to_batch", new=AsyncMock(return_value={"id": "b"})) as append, \
             patch.object(m, "mark_feedback_collected_count", new=AsyncMock()) as mark_count, \
             patch.object(m, "update_feedback_status", new=AsyncMock()):
            await m.process_feedback_collection_task(row)
        append.assert_awaited_once()
        units = append.call_args.args[3]
        assert [u["content"] for u in units] == ["새 피드백 1", "새 피드백 2"]
        mark_count.assert_awaited_once_with("t1", 3)

    @pytest.mark.asyncio
    async def test_batch_is_closed_before_classification(self):
        from core import feedback_batch_manager as m
        batch = {"id": "b1", "collected_items": [{"todo_id": f"t{i}", "content": "x"} for i in range(5)],
                 "first_collected_at": "2026-09-22T00:00:00+00:00"}
        order = []
        claim = AsyncMock(side_effect=lambda bid: order.append("claim") or {**batch, "status": "CLASSIFYING"})
        process = AsyncMock(side_effect=lambda b: order.append(("classify", b["status"])))
        sleep = AsyncMock(side_effect=asyncio.CancelledError)
        with patch.object(m, "fetch_collecting_batches", new=AsyncMock(return_value=[batch])), \
             patch.object(m, "claim_batch_for_classification", new=claim), \
             patch.object(m, "_process_triggered_batch", new=process), \
             patch.object(m, "fetch_stale_classifying_batches", new=AsyncMock(return_value=[])), \
             patch.object(m.asyncio, "sleep", new=sleep):
            await m.start_feedback_batch_trigger(interval=1)
        assert order == ["claim", ("classify", "CLASSIFYING")]

    @pytest.mark.asyncio
    async def test_failed_classification_is_retried_not_discarded(self):
        from core import feedback_batch_manager as m
        batch = {"id": "b1", "collected_items": [{"todo_id": "t1", "content": "VIP는 20% 할인"}]}
        with patch.object(m, "classify_and_extract_proposal", new=AsyncMock(return_value=None)), \
             patch.object(m, "mark_batch_discarded", new=AsyncMock()) as discard, \
             patch.object(m, "touch_classifying_batch") as touch:
            await m._process_triggered_batch(batch)
        discard.assert_not_called()
        touch.assert_called_once_with("b1")
