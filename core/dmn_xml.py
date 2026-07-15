"""JSON dmn_decisions/dmn_rules -> DMN 1.3 XML 변환.

proc_def.definition에 저장된 dmn_decisions/dmn_rules(JSON, 06-dmn.md 컨벤션)를
기계적으로 표준 DMN 1.3 XML로 옮긴다. JSON이 유일한 소스이고 XML은 그로부터
파생된 표현일 뿐이다 — XML을 독립적으로 직접 작성/수정하는 경로는 없다
(add-feedback-proposal-apply design.md Decision 7).
"""

from typing import Any, Dict, List
from xml.etree import ElementTree as ET
from xml.dom import minidom

DMN_NAMESPACE = "https://www.omg.org/spec/DMN/20191111/MODEL/"


def dmn_decisions_rules_to_xml(
    decisions: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    proc_def_id: str = "",
) -> str:
    """decisions/rules(둘 다 proc_def.definition의 dmn_decisions/dmn_rules 형태)를
    DMN 1.3 XML 문서 하나로 직렬화한다.

    decision마다 하나의 decisionTable을 만들고, 그 decision_id에 속한 rule들을
    행(rule)으로 채운다. 각 행은 condition(또는 when)을 단일 입력, target(또는
    then)을 단일 출력으로 매핑한다 — 이 시스템의 rule 표현이 자연어 위주라
    엄밀한 FEEL 표현식 대신 텍스트 그대로 옮긴다.
    """
    root = ET.Element("definitions", {
        "xmlns": DMN_NAMESPACE,
        "id": f"definitions_{proc_def_id or 'process'}",
        "name": proc_def_id or "process",
        "namespace": f"https://process-gpt/{proc_def_id or 'process'}",
    })

    rules_by_decision: Dict[str, List[Dict[str, Any]]] = {}
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        rules_by_decision.setdefault(rule.get("decision_id", ""), []).append(rule)

    for decision in decisions or []:
        if not isinstance(decision, dict):
            continue
        decision_id = decision.get("decision_id", "")
        decision_el = ET.SubElement(root, "decision", {
            "id": decision_id,
            "name": decision.get("name", ""),
        })
        if decision.get("description"):
            description_el = ET.SubElement(decision_el, "description")
            description_el.text = decision["description"]

        table_el = ET.SubElement(decision_el, "decisionTable", {
            "id": f"decisionTable_{decision_id}",
            "hitPolicy": "FIRST",
        })
        input_el = ET.SubElement(table_el, "input", {"id": f"input_{decision_id}", "label": "조건"})
        input_expression_el = ET.SubElement(input_el, "inputExpression", {"typeRef": "string"})
        ET.SubElement(input_expression_el, "text").text = "condition"
        ET.SubElement(table_el, "output", {"id": f"output_{decision_id}", "label": "결과", "typeRef": "string"})

        for rule in rules_by_decision.get(decision_id, []):
            rule_id = rule.get("rule_id", "")
            rule_el = ET.SubElement(table_el, "rule", {"id": rule_id})
            input_entry_el = ET.SubElement(rule_el, "inputEntry", {"id": f"{rule_id}_in"})
            ET.SubElement(input_entry_el, "text").text = rule.get("condition") or rule.get("when", "")
            output_entry_el = ET.SubElement(rule_el, "outputEntry", {"id": f"{rule_id}_out"})
            ET.SubElement(output_entry_el, "text").text = rule.get("target") or rule.get("then", "")

    raw_xml = ET.tostring(root, encoding="unicode")
    return minidom.parseString(raw_xml).toprettyxml(indent="  ")
