"""
DMN_RULE artifact 병합이 기존 decision을 **이름으로** 찾아 고치는지 검증한다.

대상 모듈:
- core.database.merge_dmn_artifact_into_definition

배경: 예전에는 artifact의 decision 이름으로 만든 id(dmn_decision_<slug>)가
기존 목록에 있는지만 확인했다. DMN 편집기로 만든 decision은 id가 "Decision_1"
같은 값이라 이름이 똑같아도 매칭되지 않았고, 그래서 "교통비 한도를 5만원에서
7만원으로 고쳐 달라"는 피드백이 기존 결정을 고치는 대신 같은 이름의 결정을
하나 더 만들었다. 라이브 DMN에 한도가 서로 다른 동명 결정 두 개가 남아
어느 쪽이 적용될지 알 수 없게 된다.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import merge_dmn_artifact_into_definition


def _editor_authored_definition():
    """DMN 편집기가 만든 정의 — decision_id가 편집기 컨벤션(Decision_1)이다."""
    return {
        "dmn_decisions": [
            {
                "decision_id": "Decision_1",
                "name": "경비 한도 승인 판단",
                "description": "경비유형별 1건 한도로 승인구분을 정한다.",
            }
        ],
        "dmn_rules": [
            {
                "rule_id": "rule_1",
                "decision_id": "Decision_1",
                "decision_name": "경비 한도 승인 판단",
                "when": "교통비이고 50000 이하",
                "then": "자동승인",
                "condition": '"교통비"',
                "target": None,
            }
        ],
    }


def _correction_artifact():
    """"교통비 한도는 7만원" 이라는 피드백에서 나온 규칙 보정 artifact."""
    return {
        "decision": {
            "name": "경비 한도 승인 판단",
            "description": "카테고리/금액으로 승인구분을 정한다.",
        },
        "rules": [
            {
                "when": "교통비이고 건별 70000 이하",
                "then": "승인구분 = 한도 내",
                "condition": "expense.category == '교통비' && expense.amount_per_txn <= 70000",
                "target": "승인구분",
            },
            {
                "when": "교통비이고 건별 70000 초과",
                "then": "승인구분 = 팀장 승인 필요",
                "condition": "expense.category == '교통비' && expense.amount_per_txn > 70000",
                "target": "승인구분",
            },
        ],
    }


class TestMergeMatchesExistingDecisionByName:
    def test_does_not_create_a_second_decision_with_the_same_name(self):
        merged = merge_dmn_artifact_into_definition(
            _editor_authored_definition(), _correction_artifact()
        )
        names = [d["name"] for d in merged["dmn_decisions"]]
        assert names.count("경비 한도 승인 판단") == 1, (
            "이름이 같은 decision이 두 개가 됐다 — 편집기가 만든 기존 결정을 "
            f"고치지 않고 새로 추가했다: {merged['dmn_decisions']}"
        )

    def test_reuses_the_existing_editor_decision_id(self):
        merged = merge_dmn_artifact_into_definition(
            _editor_authored_definition(), _correction_artifact()
        )
        assert merged["dmn_decisions"][0]["decision_id"] == "Decision_1"

    def test_new_rules_attach_to_the_existing_decision(self):
        merged = merge_dmn_artifact_into_definition(
            _editor_authored_definition(), _correction_artifact()
        )
        added = [r for r in merged["dmn_rules"] if r["rule_id"] != "rule_1"]
        assert added, "새 규칙이 하나도 붙지 않았다"
        assert all(r["decision_id"] == "Decision_1" for r in added), (
            "새 규칙이 기존 결정이 아니라 새로 만든 결정에 붙었다: "
            f"{[r['decision_id'] for r in added]}"
        )

    def test_existing_rules_are_kept(self):
        merged = merge_dmn_artifact_into_definition(
            _editor_authored_definition(), _correction_artifact()
        )
        assert any(r["rule_id"] == "rule_1" for r in merged["dmn_rules"])

    def test_name_match_ignores_spacing_differences(self):
        definition = _editor_authored_definition()
        definition["dmn_decisions"][0]["name"] = "경비  한도 승인  판단"
        merged = merge_dmn_artifact_into_definition(definition, _correction_artifact())
        assert len(merged["dmn_decisions"]) == 1, (
            "공백만 다른 같은 이름인데 결정이 새로 생겼다: " f"{merged['dmn_decisions']}"
        )


class TestMergeStillWorksWithoutAnExistingDecision:
    def test_creates_the_decision_when_none_matches(self):
        merged = merge_dmn_artifact_into_definition(
            {"dmn_decisions": [], "dmn_rules": []}, _correction_artifact()
        )
        assert len(merged["dmn_decisions"]) == 1
        assert merged["dmn_decisions"][0]["decision_id"] == "dmn_decision_경비_한도_승인_판단"
        assert len(merged["dmn_rules"]) == 2

    def test_unrelated_existing_decision_is_left_alone(self):
        definition = {
            "dmn_decisions": [
                {"decision_id": "Decision_9", "name": "배송비 결정", "description": ""}
            ],
            "dmn_rules": [],
        }
        merged = merge_dmn_artifact_into_definition(definition, _correction_artifact())
        ids = {d["decision_id"] for d in merged["dmn_decisions"]}
        assert "Decision_9" in ids
        assert "dmn_decision_경비_한도_승인_판단" in ids
