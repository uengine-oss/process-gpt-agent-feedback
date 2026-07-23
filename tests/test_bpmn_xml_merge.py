"""
라이브 proc_def.bpmn XML에 PROCESS_DEFINITION artifact 병합 결과 반영 테스트

대상 모듈:
- core.bpmn_xml.merge_process_definition_artifact_into_xml
"""

import sys
import os
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bpmn_xml import merge_process_definition_artifact_into_xml
from core.database import merge_process_definition_artifact_into_definition


_LIVE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="definitions_proc1">
  <bpmn:process id="proc1" isExecutable="true">
    <bpmn:task id="activity_review" name="검토"/>
    <bpmn:sequenceFlow id="seq_start_review" sourceRef="start" targetRef="activity_review"/>
  </bpmn:process>
</bpmn:definitions>
"""


def _live_definition():
    return {
        "version": "1.0",
        "activities": [{"id": "activity_review", "name": "검토", "role": "팀장"}],
        "sequences": [{"id": "seq_start_review", "source": "start", "target": "activity_review"}],
        "gateways": [],
    }


def _process_el(xml_text):
    root = ET.fromstring(xml_text)
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "process":
            return el
    return None


class TestMergeProcessDefinitionIntoXml:
    def test_add_activity_clones_existing_task_tag(self):
        live_definition = _live_definition()
        artifact = {
            "activities": [
                {"change_type": "ADD", "id": "activity_approve", "name": "승인", "role": "임원"},
            ]
        }
        merged, _ = merge_process_definition_artifact_into_definition(live_definition, artifact)

        result = merge_process_definition_artifact_into_xml(_LIVE_XML, live_definition, merged)

        process_el = _process_el(result)
        ids = [el.get("id") for el in process_el]
        assert "activity_approve" in ids
        new_el = [el for el in process_el if el.get("id") == "activity_approve"][0]
        assert new_el.tag.endswith("}task")
        assert new_el.get("name") == "승인"

    def test_modify_updates_name_in_place_without_new_element(self):
        live_definition = _live_definition()
        artifact = {
            "activities": [
                {"change_type": "MODIFY", "id": "activity_review", "name": "1차 검토"},
            ]
        }
        merged, _ = merge_process_definition_artifact_into_definition(live_definition, artifact)

        result = merge_process_definition_artifact_into_xml(_LIVE_XML, live_definition, merged)

        process_el = _process_el(result)
        matching = [el for el in process_el if el.get("id") == "activity_review"]
        assert len(matching) == 1
        assert matching[0].get("name") == "1차 검토"

    def test_add_gateway_uses_type_as_tag(self):
        live_definition = _live_definition()
        artifact = {
            "gateways": [
                {"change_type": "ADD", "type": "exclusiveGateway", "note": "금액 분기"},
            ]
        }
        merged, _ = merge_process_definition_artifact_into_definition(live_definition, artifact)

        result = merge_process_definition_artifact_into_xml(_LIVE_XML, live_definition, merged)

        process_el = _process_el(result)
        gateways = [el for el in process_el if el.tag.endswith("}exclusiveGateway")]
        assert len(gateways) == 1

    def test_add_sequence_sets_source_and_target_ref(self):
        live_definition = _live_definition()
        artifact = {
            "sequences": [
                {"change_type": "ADD", "from": "activity_review", "to": "end_event", "condition": ""},
            ]
        }
        merged, _ = merge_process_definition_artifact_into_definition(live_definition, artifact)

        result = merge_process_definition_artifact_into_xml(_LIVE_XML, live_definition, merged)

        process_el = _process_el(result)
        new_flows = [
            el for el in process_el
            if el.tag.endswith("}sequenceFlow") and el.get("sourceRef") == "activity_review"
        ]
        assert len(new_flows) == 1
        assert new_flows[0].get("targetRef") == "end_event"

    def test_empty_live_xml_returns_none(self):
        assert merge_process_definition_artifact_into_xml("", {}, {}) is None
        assert merge_process_definition_artifact_into_xml(None, {}, {}) is None

    def test_malformed_xml_returns_none_without_raising(self):
        assert merge_process_definition_artifact_into_xml("<not valid xml", {}, {}) is None

    def test_xml_without_process_element_returns_none(self):
        assert merge_process_definition_artifact_into_xml("<root/>", {}, {}) is None

    def test_no_changes_leaves_existing_elements_untouched(self):
        live_definition = _live_definition()
        merged, _ = merge_process_definition_artifact_into_definition(live_definition, {})

        result = merge_process_definition_artifact_into_xml(_LIVE_XML, live_definition, merged)

        process_el = _process_el(result)
        assert len(list(process_el)) == 2
