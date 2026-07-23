"""라이브 proc_def.bpmn XML에 PROCESS_DEFINITION artifact 병합 결과를 반영.

merge_process_definition_artifact_into_definition(core.database)이 만드는 건
flattened JSON(activities/sequences/gateways) 사본일 뿐이다. 이 모듈은 그 JSON
병합 전/후(live_definition/merged_definition)를 id로 diff해서 실제로 무엇이
새로 생겼고(ADD) 무엇이 바뀌었는지(MODIFY) 가려낸 뒤, 같은 변경을 라이브 BPMN
XML 트리에도 적용한다.

표준 BPMN 태그를 새로 지어내지 않는다: 새 요소는 문서 안에 이미 있는 같은 종류
요소의 태그(네임스페이스 포함)를 템플릿으로 복제한다 — 그런 템플릿이 전혀 없을
때만 프로세스 요소 자신의 네임스페이스로 최소한의 기본 태그를 만든다. 파싱에
실패하거나 <process> 요소를 찾지 못하면 None을 반환해 호출부가 JSON snapshot
으로 폴백하게 한다.
"""

import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.dom import minidom

_ARRAY_TAG_DEFAULTS = {
    "activities": "task",
    "sequences": "sequenceFlow",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace_of(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _register_namespaces(xml_text: str) -> None:
    head = xml_text[:2000]
    for prefix, uri in re.findall(r'xmlns(:[A-Za-z0-9_.-]+)?="([^"]+)"', head):
        ET.register_namespace(prefix.lstrip(":"), uri)


def _find_process_element(root: ET.Element) -> Optional[ET.Element]:
    if _local_name(root.tag) == "process":
        return root
    for el in root.iter():
        if _local_name(el.tag) == "process":
            return el
    return None


def _find_by_id(root: ET.Element, element_id: str) -> Optional[ET.Element]:
    for el in root.iter():
        if el.get("id") == element_id:
            return el
    return None


def _diff_by_id(live_list: Optional[List[Dict[str, Any]]], merged_list: Optional[List[Dict[str, Any]]]):
    live_by_id = {
        el.get("id"): el for el in (live_list or []) if isinstance(el, dict) and el.get("id")
    }
    new_items: List[Dict[str, Any]] = []
    changed_items: List[Dict[str, Any]] = []
    for el in merged_list or []:
        if not isinstance(el, dict) or not el.get("id"):
            continue
        eid = el["id"]
        if eid not in live_by_id:
            new_items.append(el)
        elif el != live_by_id[eid]:
            changed_items.append(el)
    return new_items, changed_items


def _documentation_text(element: Dict[str, Any], skip_keys) -> str:
    return " / ".join(f"{k}: {v}" for k, v in element.items() if k not in skip_keys and v)


def _template_tag_for(root: ET.Element, live_list: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    for existing in live_list or []:
        if not isinstance(existing, dict) or not existing.get("id"):
            continue
        existing_el = _find_by_id(root, existing["id"])
        if existing_el is not None:
            return existing_el.tag
    return None


def _append_new_element(
    process_el: ET.Element,
    array_key: str,
    element: Dict[str, Any],
    template_tag: Optional[str],
    fallback_ns: str,
) -> ET.Element:
    if array_key == "gateways":
        local = (element.get("type") or "exclusiveGateway").strip() or "exclusiveGateway"
        tag = f"{{{fallback_ns}}}{local}" if fallback_ns else local
    elif template_tag:
        tag = template_tag
    else:
        local = _ARRAY_TAG_DEFAULTS.get(array_key, "task")
        tag = f"{{{fallback_ns}}}{local}" if fallback_ns else local

    new_el = ET.SubElement(process_el, tag)
    new_el.set("id", element["id"])

    if array_key == "sequences":
        if element.get("source"):
            new_el.set("sourceRef", element["source"])
        if element.get("target"):
            new_el.set("targetRef", element["target"])
        if element.get("condition"):
            new_el.set("name", element["condition"])
        doc_text = _documentation_text(element, {"id", "source", "target", "condition"})
    else:
        if element.get("name"):
            new_el.set("name", element["name"])
        doc_text = _documentation_text(element, {"id", "name", "type"})

    if doc_text:
        ns = fallback_ns or _namespace_of(tag)
        doc_tag = f"{{{ns}}}documentation" if ns else "documentation"
        ET.SubElement(new_el, doc_tag).text = doc_text

    return new_el


def merge_process_definition_artifact_into_xml(
    xml_text: str,
    live_definition: Dict[str, Any],
    merged_definition: Dict[str, Any],
) -> Optional[str]:
    """라이브 BPMN XML에 activities/sequences/gateways 변경사항을 반영한 XML 문자열을
    반환한다. 파싱 실패, <process> 요소 없음 등 병합 불가 상황이면 None을 반환한다
    (호출부가 JSON snapshot으로 폴백).
    """
    if not xml_text or not xml_text.strip():
        return None

    try:
        _register_namespaces(xml_text)
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    process_el = _find_process_element(root)
    if process_el is None:
        return None

    fallback_ns = _namespace_of(process_el.tag)

    for array_key in ("activities", "sequences", "gateways"):
        new_items, changed_items = _diff_by_id(
            live_definition.get(array_key), merged_definition.get(array_key)
        )

        for element in changed_items:
            target_el = _find_by_id(root, element["id"])
            if target_el is None:
                new_items.append(element)
                continue
            if array_key == "sequences":
                if element.get("condition"):
                    target_el.set("name", element["condition"])
            elif element.get("name"):
                target_el.set("name", element["name"])

        template_tag = _template_tag_for(root, live_definition.get(array_key))
        for element in new_items:
            _append_new_element(process_el, array_key, element, template_tag, fallback_ns)

    raw_xml = ET.tostring(root, encoding="unicode")
    try:
        return minidom.parseString(raw_xml).toprettyxml(indent="  ")
    except Exception:
        return raw_xml
