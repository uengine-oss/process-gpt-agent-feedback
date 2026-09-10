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
        empty = {"dmn_decisions": [], "dmn_rules": [], "dmn_input_data": []}
        assert xml_to_dmn_decisions_rules("") == empty
        assert xml_to_dmn_decisions_rules(None) == empty

    def test_malformed_xml_returns_empty_structure_without_raising(self):
        assert xml_to_dmn_decisions_rules("<not valid xml") == {
            "dmn_decisions": [],
            "dmn_rules": [],
            "dmn_input_data": [],
        }


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


class TestDmnXmlPreservesTableShape:
    """왕복이 표의 생김새를 잃지 않는지 고정한다.

    실제 사고: 피드백이 `할인율 산정` 결정 하나를 더하려고 DMN 전체를 왕복시켰는데,
    그 과정에서 표들의 입력 열이 리터럴 `condition` 한 칸으로 뭉개지고 InputData 가
    사라졌다. 그렇게 저장된 표는 실행 시 어떤 입력에도 맞지 않아, 병합 전 검증이
    "기존 동작 6개 단계가 깨진다" 고 잡아냈다.
    """

    SOURCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" id="definitions_1" name="고객 혜택">
  <inputData id="inputdata_customer_grade" name="고객등급" />
  <inputData id="inputdata_purchase_amount" name="구매금액" />
  <decision id="decision_benefit_base" name="기본 혜택 결정">
    <decisionTable id="t1" hitPolicy="FIRST">
      <input id="i1"><inputExpression typeRef="string"><text>고객등급</text></inputExpression></input>
      <output id="o1" label="기본혜택" typeRef="number" />
      <rule id="r1"><inputEntry id="r1i"><text>"VIP"</text></inputEntry><outputEntry id="r1o"><text>20</text></outputEntry></rule>
      <rule id="r2"><inputEntry id="r2i"><text>"GOLD"</text></inputEntry><outputEntry id="r2o"><text>10</text></outputEntry></rule>
    </decisionTable>
  </decision>
  <decision id="decision_benefit_extra" name="추가 혜택 결정">
    <decisionTable id="t2" hitPolicy="FIRST">
      <input id="i2"><inputExpression typeRef="string"><text>고객등급</text></inputExpression></input>
      <input id="i3"><inputExpression typeRef="number"><text>구매금액</text></inputExpression></input>
      <output id="o2" label="추가혜택" typeRef="number" />
      <rule id="r3">
        <inputEntry id="r3i"><text>"VIP"</text></inputEntry>
        <inputEntry id="r3i2"><text>&lt; 500000</text></inputEntry>
        <outputEntry id="r3o"><text>10</text></outputEntry>
      </rule>
      <rule id="r4">
        <inputEntry id="r4i"><text>"VIP"</text></inputEntry>
        <inputEntry id="r4i2"><text>&gt;= 1000000</text></inputEntry>
        <outputEntry id="r4o"><text>23</text></outputEntry>
      </rule>
    </decisionTable>
  </decision>
</definitions>"""

    def _roundtrip(self):
        parsed = xml_to_dmn_decisions_rules(self.SOURCE_XML)
        xml_again = dmn_decisions_rules_to_xml(
            parsed["dmn_decisions"], parsed["dmn_rules"], proc_def_id="customer_benefit_decision"
        )
        return parsed, xml_again, xml_to_dmn_decisions_rules(xml_again)

    def test_input_expressions_survive_the_roundtrip(self):
        """표가 읽던 변수 이름이 남아야 한다. 리터럴 `condition` 으로 바뀌면 실행 시
        아무 입력도 바인딩되지 않아 표 전체가 죽는다."""
        _, xml_again, reparsed = self._roundtrip()

        assert "<text>고객등급</text>" in xml_again
        assert "<text>구매금액</text>" in xml_again
        assert "<text>condition</text>" not in xml_again

        base = next(d for d in reparsed["dmn_decisions"] if d["decision_id"] == "decision_benefit_base")
        assert [i["expression"] for i in base["inputs"]] == ["고객등급"]

    def test_multi_column_rules_keep_every_condition(self):
        """다중 입력 표에서 첫 칸만 남기면 `"VIP" AND < 500000` 과
        `"VIP" AND >= 1000000` 이 같은 행이 되어 뒤 행이 죽는다."""
        _, _, reparsed = self._roundtrip()

        extra = [r for r in reparsed["dmn_rules"] if r["decision_id"] == "decision_benefit_extra"]
        assert [r["conditions"] for r in extra] == [['"VIP"', "< 500000"], ['"VIP"', ">= 1000000"]]
        assert [r["target"] for r in extra] == ["10", "23"]

    def test_output_and_hit_policy_survive(self):
        _, xml_again, reparsed = self._roundtrip()

        base = next(d for d in reparsed["dmn_decisions"] if d["decision_id"] == "decision_benefit_base")
        assert base["output"]["label"] == "기본혜택"
        assert base["output"]["type_ref"] == "number"
        assert base["hit_policy"] == "FIRST"
        assert 'typeRef="number"' in xml_again

    def test_new_decision_without_columns_still_gets_a_usable_table(self):
        """피드백이 새로 만든 decision 은 열 정보가 없다 — 예전처럼 한 칸짜리 표로 쓴다."""
        xml_text = dmn_decisions_rules_to_xml(
            [{"decision_id": "dmn_decision_할인율_산정", "name": "할인율 산정"}],
            [{"rule_id": "r1", "decision_id": "dmn_decision_할인율_산정", "when": "주문금액 100만원 이상", "then": "5%"}],
            proc_def_id="dmn_test",
        )

        assert "<text>condition</text>" in xml_text
        parsed = xml_to_dmn_decisions_rules(xml_text)
        assert parsed["dmn_rules"][0]["condition"] == "주문금액 100만원 이상"

    def test_input_data_definitions_survive(self):
        """표가 읽는 변수의 출처(DRD 입력 노드). 빠뜨리면 규칙 하나 더한 draft 가
        입력 데이터를 통째로 지운 것으로 저장된다."""
        parsed = xml_to_dmn_decisions_rules(self.SOURCE_XML)
        assert [i["name"] for i in parsed["dmn_input_data"]] == ["고객등급", "구매금액"]

        xml_again = dmn_decisions_rules_to_xml(
            parsed["dmn_decisions"],
            parsed["dmn_rules"],
            proc_def_id="customer_benefit_decision",
            input_data=parsed["dmn_input_data"],
        )

        assert '<inputData id="inputdata_customer_grade" name="고객등급"' in xml_again
        assert '<inputData id="inputdata_purchase_amount" name="구매금액"' in xml_again
        # DRD 에 그려지려면 좌표도 있어야 한다.
        assert 'dmnElementRef="inputdata_customer_grade"' in xml_again
        assert [i["name"] for i in xml_to_dmn_decisions_rules(xml_again)["dmn_input_data"]] == ["고객등급", "구매금액"]

    def test_missing_conditions_are_filled_with_any(self):
        """열이 둘인데 조건이 하나뿐이면 남는 열은 `-`(아무 값이나)로 채운다 —
        빈 칸으로 두면 파서마다 다르게 읽힌다."""
        xml_text = dmn_decisions_rules_to_xml(
            [{
                "decision_id": "d1",
                "name": "두 열 표",
                "inputs": [
                    {"id": "i1", "label": "등급", "expression": "고객등급"},
                    {"id": "i2", "label": "금액", "expression": "구매금액"},
                ],
            }],
            [{"rule_id": "r1", "decision_id": "d1", "condition": '"VIP"', "target": "20"}],
            proc_def_id="dmn_test",
        )

        parsed = xml_to_dmn_decisions_rules(xml_text)
        assert parsed["dmn_rules"][0]["conditions"] == ['"VIP"', "-"]
