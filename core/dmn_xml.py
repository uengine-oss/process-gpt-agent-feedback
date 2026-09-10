"""JSON dmn_decisions/dmn_rules <-> DMN 1.3 XML 변환.

proc_def.definition에 저장된 dmn_decisions/dmn_rules(JSON, 06-dmn.md 컨벤션)를
기계적으로 표준 DMN 1.3 XML로 옮긴다. JSON이 유일한 소스이고 XML은 그로부터
파생된 표현일 뿐이다 — XML을 독립적으로 직접 작성/수정하는 경로는 없다
(add-feedback-proposal-apply design.md Decision 7).

**표의 입력 열은 반드시 보존한다.**
피드백으로 규칙 하나를 더할 때도 이 두 함수는 DMN 전체를 한 번 왕복한다. 예전에는
왕복하면서 표의 입력 열이 통째로 사라지고(`고객등급`, `구매금액` → 리터럴 `condition`
한 칸), 다중 입력 표는 첫 칸만 남아 `"VIP" AND < 500000` 과 `"VIP" AND >= 1000000` 이
같은 행으로 뭉개졌다. 그렇게 저장된 표는 실행 시 어떤 입력에도 맞지 않아 늘 "매칭 없음"
이 된다 — 실제로 병합 전 검증이 기존 동작 6단계가 깨진다고 잡아낸 사고다.
그래서 decision 은 `inputs`/`output`/`hit_policy` 를, rule 은 열 수만큼의 `conditions` 를
JSON 쪽에도 들고 다닌다. 그 정보가 없는(=피드백이 새로 만든) decision 만 예전처럼
한 칸짜리 표로 쓴다.
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
# 입력 열 정보가 없는 decision(피드백이 새로 만든 것)에 쓰는 한 칸짜리 표.
DEFAULT_INPUT_LABEL = "조건"
DEFAULT_INPUT_EXPRESSION = "condition"
DEFAULT_OUTPUT_LABEL = "결과"
DEFAULT_HIT_POLICY = "FIRST"
# DMN 에서 "이 열은 아무 값이나" 를 뜻하는 표기. 열 수보다 조건이 적을 때 채운다.
ANY_ENTRY = "-"

_DMNDI_COLUMNS = 3
_DMNDI_H_SPACING = 220
_DMNDI_V_SPACING = 150
_DMNDI_SHAPE_WIDTH = 180
_DMNDI_SHAPE_HEIGHT = 80


def _decision_inputs(decision: Dict[str, Any], decision_id: str) -> List[Dict[str, str]]:
    """이 decision 표의 입력 열들.

    JSON 에 열 정보가 있으면 그대로 쓴다 — 여기서 리터럴 하나로 덮어쓰면 표가 읽던
    변수(`고객등급` 등)를 잃어 실행 시 어떤 입력에도 맞지 않게 된다.
    """
    columns: List[Dict[str, str]] = []
    for index, raw in enumerate(decision.get("inputs") or []):
        if not isinstance(raw, dict):
            continue
        expression = str(raw.get("expression") or raw.get("expr") or raw.get("label") or "").strip()
        if not expression:
            continue
        columns.append({
            "id": str(raw.get("id") or f"input_{decision_id}_{index + 1}"),
            "label": str(raw.get("label") or expression),
            "expression": expression,
            "type_ref": str(raw.get("type_ref") or raw.get("typeRef") or "string"),
        })
    if columns:
        return columns
    return [{
        "id": f"input_{decision_id}",
        "label": DEFAULT_INPUT_LABEL,
        "expression": DEFAULT_INPUT_EXPRESSION,
        "type_ref": "string",
    }]


def _rule_entries(rule: Dict[str, Any], column_count: int) -> List[str]:
    """규칙 한 행이 열마다 갖는 조건 텍스트.

    열 수와 조건 수가 어긋나면 남는 열은 `-`(아무 값이나)로 채운다 — 빈 칸으로 두면
    파서마다 다르게 읽히고, 조건을 지어내면 없던 제약이 생긴다.
    """
    raw = rule.get("conditions")
    entries = [str(item or "") for item in raw] if isinstance(raw, list) else []
    if not entries:
        entries = [str(rule.get("condition") or rule.get("when") or "")]
    entries = entries[:column_count]
    while len(entries) < column_count:
        entries.append(ANY_ENTRY)
    return entries


def dmn_decisions_rules_to_xml(
    decisions: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    proc_def_id: str = "",
    input_data: List[Dict[str, Any]] | None = None,
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

    # 입력 데이터 정의(DRD 의 타원 노드). 이걸 빠뜨리면 표가 읽는 변수의 출처가 사라지고,
    # 변경 이력에는 "입력 데이터 삭제" 로 남는다 — 규칙 하나 더했을 뿐인데.
    input_data_ids: List[str] = []
    for index, item in enumerate(input_data or []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or f"inputdata_{index + 1}")
        input_data_ids.append(item_id)
        input_data_el = ET.SubElement(root, "inputData", {"id": item_id, "name": str(item.get("name") or "")})
        variable = item.get("variable") if isinstance(item.get("variable"), dict) else {}
        ET.SubElement(input_data_el, "variable", {
            "id": str(variable.get("id") or f"var_{item_id}"),
            "name": str(variable.get("name") or item.get("name") or ""),
            "typeRef": str(variable.get("type_ref") or variable.get("typeRef") or "string"),
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
            "hitPolicy": decision.get("hit_policy") or DEFAULT_HIT_POLICY,
        })

        inputs = _decision_inputs(decision, decision_id)
        for column in inputs:
            input_el = ET.SubElement(table_el, "input", {
                "id": column["id"],
                "label": column["label"],
            })
            input_expression_el = ET.SubElement(input_el, "inputExpression", {"typeRef": column["type_ref"]})
            ET.SubElement(input_expression_el, "text").text = column["expression"]

        output = decision.get("output") if isinstance(decision.get("output"), dict) else {}
        output_attrs = {
            "id": output.get("id") or f"output_{decision_id}",
            "label": output.get("label") or DEFAULT_OUTPUT_LABEL,
            "typeRef": output.get("type_ref") or output.get("typeRef") or "string",
        }
        if output.get("name"):
            output_attrs["name"] = output["name"]
        ET.SubElement(table_el, "output", output_attrs)

        for rule in rules_by_decision.get(decision_id, []):
            rule_id = rule.get("rule_id", "")
            rule_el = ET.SubElement(table_el, "rule", {"id": rule_id})
            for index, entry in enumerate(_rule_entries(rule, len(inputs))):
                input_entry_el = ET.SubElement(rule_el, "inputEntry", {
                    "id": f"{rule_id}_in" if index == 0 else f"{rule_id}_in{index + 1}",
                })
                ET.SubElement(input_entry_el, "text").text = entry
            output_entry_el = ET.SubElement(rule_el, "outputEntry", {"id": f"{rule_id}_out"})
            ET.SubElement(output_entry_el, "text").text = rule.get("target") or rule.get("then", "")

    if decision_ids_in_order or input_data_ids:
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

        # 입력 데이터는 결정 아래쪽 줄에 늘어놓는다 — 좌표가 없으면 dmn-js 가 그리지 못한다.
        base_row = (len(decision_ids_in_order) + _DMNDI_COLUMNS - 1) // _DMNDI_COLUMNS
        for idx, item_id in enumerate(input_data_ids):
            col = idx % _DMNDI_COLUMNS
            row = base_row + idx // _DMNDI_COLUMNS
            shape_el = ET.SubElement(diagram_el, "dmndi:DMNShape", {
                "id": f"DMNShape_{item_id}",
                "dmnElementRef": item_id,
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

    표의 입력 열(`inputs`), 출력 열(`output`), hit policy 와 규칙 행의 열별 조건
    (`conditions`)까지 함께 읽는다 — 이 정보를 버리면 다시 XML 로 쓸 때 표가 읽던
    변수를 잃고, 그렇게 저장된 표는 실행 시 어떤 입력에도 맞지 않는다.

    손실 변환 주의: rule 의 condition/when 은 같은 inputEntry 텍스트 한 칸에,
    target/then 은 같은 outputEntry 텍스트 한 칸에 합쳐서 쓴다. 역파싱은 그 둘을
    구분할 수 없으므로 condition==when, target==then 으로 동일한 값을 채운다.

    파싱 실패/빈 입력은 예외를 던지지 않고 빈 결과를 반환한다 — 호출부가 "아직 규칙
    없는 빈 DMN"으로 자연스럽게 처리하게 하기 위함이다.
    """
    empty: Dict[str, List[Dict[str, Any]]] = {"dmn_decisions": [], "dmn_rules": [], "dmn_input_data": []}
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
    input_data: List[Dict[str, Any]] = []

    for input_data_el in _findall(root, "inputData"):
        variable_el = _find(input_data_el, "variable")
        entry: Dict[str, Any] = {
            "id": input_data_el.get("id", ""),
            "name": input_data_el.get("name", ""),
        }
        if variable_el is not None:
            entry["variable"] = {
                "id": variable_el.get("id", ""),
                "name": variable_el.get("name", ""),
                "type_ref": variable_el.get("typeRef") or "string",
            }
        input_data.append(entry)

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

        # 표의 생김새(입력 열·출력 열·hit policy)를 decision 에 붙여 둔다.
        input_columns: List[Dict[str, Any]] = []
        for index, input_el in enumerate(_findall(table_el, "input")):
            expression_el = _find(input_el, "inputExpression")
            text_el = _find(expression_el, "text") if expression_el is not None else None
            expression = (text_el.text or "").strip() if text_el is not None and text_el.text else ""
            label = input_el.get("label") or ""
            input_columns.append({
                "id": input_el.get("id") or f"input_{decision_id}_{index + 1}",
                "label": label or expression,
                "expression": expression or label,
                "type_ref": (expression_el.get("typeRef") if expression_el is not None else "") or "string",
            })
        if input_columns:
            decisions[-1]["inputs"] = input_columns

        output_el = _find(table_el, "output")
        if output_el is not None:
            decisions[-1]["output"] = {
                "id": output_el.get("id") or f"output_{decision_id}",
                "label": output_el.get("label") or DEFAULT_OUTPUT_LABEL,
                "type_ref": output_el.get("typeRef") or "string",
                "name": output_el.get("name") or "",
            }
        if table_el.get("hitPolicy"):
            decisions[-1]["hit_policy"] = table_el.get("hitPolicy")

        for rule_el in _findall(table_el, "rule"):
            rule_id = rule_el.get("id", "")

            conditions: List[str] = []
            for input_entry_el in _findall(rule_el, "inputEntry"):
                text_el = _find(input_entry_el, "text")
                conditions.append((text_el.text or "") if text_el is not None and text_el.text else "")
            # 여러 열짜리 표에서 첫 칸만 남기면 `"VIP" AND < 500000` 과
            # `"VIP" AND >= 1000000` 이 같은 행으로 뭉개진다 — 열 전체를 들고 다닌다.
            condition = conditions[0] if conditions else ""

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
                "conditions": conditions,
                "target": target,
            })

    return {"dmn_decisions": decisions, "dmn_rules": rules, "dmn_input_data": input_data}
