"""
DMN XML <-> JSON 왕복 변환 테스트

대상 모듈:
- core.dmn_xml.dmn_decisions_rules_to_xml
- core.dmn_xml.xml_to_dmn_decisions_rules
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dmn_xml import dmn_decisions_rules_to_xml, xml_to_dmn_decisions_rules


def _decisions_rules():
    decisions = [
        {"decision_id": "dmn_decision_금액_분기", "name": "금액 분기", "description": "결제 금액에 따른 승인 경로 판단"},
    ]
    rules = [
        {
            "rule_id": "dmn_rule_금액_분기_1",
            "decision_id": "dmn_decision_금액_분기",
            "decision_name": "금액 분기",
            "when": "금액이 100만원 이상",
            "then": "추가 승인 필요",
            "condition": "금액이 100만원 이상",
            "target": "추가 승인 필요",
        },
    ]
    return decisions, rules


class TestDmnXmlRoundtrip:
    def test_roundtrip_preserves_decision_and_rule_ids(self):
        decisions, rules = _decisions_rules()
        xml_text = dmn_decisions_rules_to_xml(decisions, rules, proc_def_id="dmn_test")

        parsed = xml_to_dmn_decisions_rules(xml_text)

        assert len(parsed["dmn_decisions"]) == 1
        assert parsed["dmn_decisions"][0]["decision_id"] == "dmn_decision_금액_분기"
        assert parsed["dmn_decisions"][0]["name"] == "금액 분기"
        assert parsed["dmn_decisions"][0]["description"] == "결제 금액에 따른 승인 경로 판단"

        assert len(parsed["dmn_rules"]) == 1
        rule = parsed["dmn_rules"][0]
        assert rule["rule_id"] == "dmn_rule_금액_분기_1"
        assert rule["decision_id"] == "dmn_decision_금액_분기"

    def test_roundtrip_collapses_condition_and_when_to_same_value(self):
        """XML은 condition/when을 같은 텍스트 칸 하나에 합쳐 쓰므로, 역파싱하면 두 값이
        구분되지 않고 동일해진다 — 손실 변환이 의도된 동작임을 문서화한다."""
        decisions, rules = _decisions_rules()
        xml_text = dmn_decisions_rules_to_xml(decisions, rules, proc_def_id="dmn_test")

        parsed = xml_to_dmn_decisions_rules(xml_text)

        rule = parsed["dmn_rules"][0]
        assert rule["when"] == rule["condition"] == "금액이 100만원 이상"
        assert rule["then"] == rule["target"] == "추가 승인 필요"

    def test_empty_xml_returns_empty_structure(self):
        assert xml_to_dmn_decisions_rules("") == {"dmn_decisions": [], "dmn_rules": []}
        assert xml_to_dmn_decisions_rules(None) == {"dmn_decisions": [], "dmn_rules": []}

    def test_malformed_xml_returns_empty_structure_without_raising(self):
        assert xml_to_dmn_decisions_rules("<not valid xml") == {"dmn_decisions": [], "dmn_rules": []}


class TestDmnXmlDiagramInterchange:
    """dmn-js는 dmndi:DMNShape 없이는 DRD를 그리지 못해, decision이 여럿이어도 그중
    하나의 decision table만 열리고 나머지로 전환할 방법이 없다(실제 발생 사례:
    customer_benefit_decision 개선 draft가 5개 decision 중 1개만 보이던 문제).
    decision마다 DMNShape가 나오는지 확인한다."""

    def test_emits_one_dmnshape_per_decision(self):
        decisions = [
            {"decision_id": "d1", "name": "결정1"},
            {"decision_id": "d2", "name": "결정2"},
            {"decision_id": "d3", "name": "결정3"},
        ]
        rules = [
            {"rule_id": "r1", "decision_id": "d1", "when": "x", "then": "y"},
        ]

        xml_text = dmn_decisions_rules_to_xml(decisions, rules, proc_def_id="dmn_test")

        assert xml_text.count("<dmndi:DMNShape") == 3
        for decision_id in ("d1", "d2", "d3"):
            assert f'dmnElementRef="{decision_id}"' in xml_text
        assert "<dc:Bounds" in xml_text
        assert "xmlns:dmndi=" in xml_text

    def test_no_decisions_emits_no_dmndi_section(self):
        xml_text = dmn_decisions_rules_to_xml([], [], proc_def_id="dmn_test")

        assert "dmndi:DMNDI" not in xml_text

    def test_dmndi_section_does_not_break_roundtrip_parsing(self):
        decisions = [
            {"decision_id": "d1", "name": "결정1"},
            {"decision_id": "d2", "name": "결정2"},
        ]
        rules = [
            {"rule_id": "r1", "decision_id": "d1", "when": "x", "then": "y"},
        ]
        xml_text = dmn_decisions_rules_to_xml(decisions, rules, proc_def_id="dmn_test")

        parsed = xml_to_dmn_decisions_rules(xml_text)

        assert len(parsed["dmn_decisions"]) == 2
        assert {d["decision_id"] for d in parsed["dmn_decisions"]} == {"d1", "d2"}
