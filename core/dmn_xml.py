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
DMNDI_NAMESPACE = "https://www.omg.org/spec/DMN/20191111/DMNDI/"
DC_NAMESPACE = "http://www.omg.org/spec/DMN/20180521/DC/"
DI_NAMESPACE = "http://www.omg.org/spec/DMN/20180521/DI/"

# dmn-js가 여러 decision을 가진 DRD를 렌더링/탐색하려면 dmndi:DMNShape 좌표가
# 필요하다(없으면 하나의 decision table만 열리고 나머지로 이동할 방법이 없다) —
# informationRequirement 관계는 이 JSON 스키마가 추적하지 않으므로 grid 배치로
# 결정 상자 위치만 채운다.
_DMNDI_COLUMNS = 3
_DMNDI_H_SPACING = 220
_DMNDI_V_SPACING = 150
_DMNDI_SHAPE_WIDTH = 180
_DMNDI_SHAPE_HEIGHT = 80


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

    decision마다 dmndi:DMNShape도 함께 만든다(grid 배치) — dmn-js는 DMNDI 없이는
    DRD를 그리지 못해 decision이 여럿이어도 그중 하나의 decision table만 열리고
    나머지로 전환할 길이 없다(실제 발생 사례: customer_benefit_decision 개선
    draft가 5개 decision 중 1개만 보이던 문제).
    """
    root = ET.Element("definitions", {
        "xmlns": DMN_NAMESPACE,
        "xmlns:dmndi": DMNDI_NAMESPACE,
        "xmlns:dc": DC_NAMESPACE,
        "xmlns:di": DI_NAMESPACE,
        "id": f"definitions_{proc_def_id or 'process'}",
        "name": proc_def_id or "process",
        "namespace": f"https://process-gpt/{proc_def_id or 'process'}",
    })

    rules_by_decision: Dict[str, List[Dict[str, Any]]] = {}
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        rules_by_decision.setdefault(rule.get("decision_id", ""), []).append(rule)

    decision_ids_in_order: List[str] = []
    for decision in decisions or []:
        if not isinstance(decision, dict):
            continue
        decision_id = decision.get("decision_id", "")
        decision_ids_in_order.append(decision_id)
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

    if decision_ids_in_order:
        dmndi_el = ET.SubElement(root, "dmndi:DMNDI")
        diagram_el = ET.SubElement(dmndi_el, "dmndi:DMNDiagram", {
            "id": f"DMNDiagram_{proc_def_id or 'process'}",
        })
        for idx, decision_id in enumerate(decision_ids_in_order):
            col = idx % _DMNDI_COLUMNS
            row = idx // _DMNDI_COLUMNS
            shape_el = ET.SubElement(diagram_el, "dmndi:DMNShape", {
                "id": f"DMNShape_{decision_id}",
                "dmnElementRef": decision_id,
            })
            ET.SubElement(shape_el, "dc:Bounds", {
                "height": str(_DMNDI_SHAPE_HEIGHT),
                "width": str(_DMNDI_SHAPE_WIDTH),
                "x": str(60 + col * _DMNDI_H_SPACING),
                "y": str(60 + row * _DMNDI_V_SPACING),
            })

    raw_xml = ET.tostring(root, encoding="unicode")
    return minidom.parseString(raw_xml).toprettyxml(indent="  ")


def xml_to_dmn_decisions_rules(xml_text: str) -> Dict[str, List[Dict[str, Any]]]:
    """DMN 1.3 XML(dmn_decisions_rules_to_xml이 만든 형태)을 dmn_decisions/dmn_rules
    JSON으로 역파싱한다. proc_def.type='dmn' 행은 definition이 아니라 bpmn 컬럼에만
    규칙이 XML로 저장돼 있어(정의상 definition은 null), 기존 규칙을 읽으려면 이 경로가
    유일하다.

    손실 변환 주의: dmn_decisions_rules_to_xml은 rule의 condition/when을 같은
    inputEntry 텍스트 한 칸에, target/then을 같은 outputEntry 텍스트 한 칸에 합쳐서
    쓴다. 역파싱은 그 둘을 구분할 수 없으므로 condition==when, target==then으로
    동일한 값을 채운다.

    파싱 실패/빈 입력은 예외를 던지지 않고 빈 결과를 반환한다 — 호출부가 "아직 규칙
    없는 빈 DMN"으로 자연스럽게 처리하게 하기 위함이다.
    """
    empty: Dict[str, List[Dict[str, Any]]] = {"dmn_decisions": [], "dmn_rules": []}
    if not xml_text or not xml_text.strip():
        return empty

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return empty

    ns = {"dmn": DMN_NAMESPACE}

    def _find(el, tag):
        found = el.find(f"dmn:{tag}", ns)
        if found is None:
            found = el.find(tag)
        return found

    def _findall(el, tag):
        found = el.findall(f"dmn:{tag}", ns)
        if not found:
            found = el.findall(tag)
        return found

    decisions: List[Dict[str, Any]] = []
    rules: List[Dict[str, Any]] = []

    for decision_el in _findall(root, "decision"):
        decision_id = decision_el.get("id", "")
        decision_name = decision_el.get("name", "")
        description_el = _find(decision_el, "description")
        decisions.append({
            "decision_id": decision_id,
            "name": decision_name,
            "description": description_el.text if description_el is not None and description_el.text else "",
        })

        table_el = _find(decision_el, "decisionTable")
        if table_el is None:
            continue

        for rule_el in _findall(table_el, "rule"):
            rule_id = rule_el.get("id", "")

            input_entry_el = _find(rule_el, "inputEntry")
            condition = ""
            if input_entry_el is not None:
                text_el = _find(input_entry_el, "text")
                if text_el is not None and text_el.text:
                    condition = text_el.text

            output_entry_el = _find(rule_el, "outputEntry")
            target = ""
            if output_entry_el is not None:
                text_el = _find(output_entry_el, "text")
                if text_el is not None and text_el.text:
                    target = text_el.text

            rules.append({
                "rule_id": rule_id,
                "decision_id": decision_id,
                "decision_name": decision_name,
                "when": condition,
                "then": target,
                "condition": condition,
                "target": target,
            })

    return {"dmn_decisions": decisions, "dmn_rules": rules}
