"""
PROCESS_DEFINITION target 승인 시 병합 로직 테스트

대상 모듈:
- core.database.merge_process_definition_artifact_into_definition
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import merge_process_definition_artifact_into_definition


def _base_definition():
    return {
        "version": "1.0",
        "activities": [
            {"id": "activity_review", "name": "검토", "role": "팀장"},
        ],
        "sequences": [],
        "gateways": [],
    }


class TestMergeProcessDefinitionArtifact:
    def test_add_with_id_is_appended(self):
        definition = _base_definition()
        artifact = {
            "activities": [
                {"change_type": "ADD", "id": "activity_approve", "name": "승인", "role": "임원"},
            ]
        }

        merged, demoted = merge_process_definition_artifact_into_definition(definition, artifact)

        ids = [a["id"] for a in merged["activities"]]
        assert ids == ["activity_review", "activity_approve"]
        assert demoted == 0

    def test_add_without_id_generates_one(self):
        definition = _base_definition()
        artifact = {
            "gateways": [
                {"change_type": "ADD", "type": "exclusiveGateway", "name": "금액 분기"},
            ]
        }

        merged, demoted = merge_process_definition_artifact_into_definition(definition, artifact)

        assert len(merged["gateways"]) == 1
        assert merged["gateways"][0]["id"] == "gateway_금액_분기"
        assert demoted == 0

    def test_add_with_colliding_id_is_deduped(self):
        definition = _base_definition()
        artifact = {
            "activities": [
                {"change_type": "ADD", "id": "activity_review", "name": "검토(중복)", "role": "팀장"},
            ]
        }

        merged, demoted = merge_process_definition_artifact_into_definition(definition, artifact)

        assert len(merged["activities"]) == 1
        assert merged["activities"][0]["name"] == "검토"  # 원본 유지, 덮어쓰지 않음
        assert demoted == 0

    def test_modify_with_matching_id_updates_in_place(self):
        definition = _base_definition()
        artifact = {
            "activities": [
                {"change_type": "MODIFY", "id": "activity_review", "role": "본부장"},
            ]
        }

        merged, demoted = merge_process_definition_artifact_into_definition(definition, artifact)

        assert len(merged["activities"]) == 1
        assert merged["activities"][0]["role"] == "본부장"
        assert merged["activities"][0]["name"] == "검토"  # 명시 안 된 필드는 유지
        assert demoted == 0

    def test_modify_with_unmatched_id_is_demoted_to_add(self):
        definition = _base_definition()
        artifact = {
            "activities": [
                {"change_type": "MODIFY", "id": "activity_invented", "name": "인수인계", "role": "담당자"},
            ]
        }

        merged, demoted = merge_process_definition_artifact_into_definition(definition, artifact)

        assert demoted == 1
        ids = [a["id"] for a in merged["activities"]]
        assert "activity_review" in ids
        # 지어낸 id를 그대로 쓰지 않고 새로 생성한다
        assert "activity_invented" not in ids
        assert len(ids) == 2

    def test_repeated_merge_of_unresolved_modify_does_not_duplicate(self):
        definition = _base_definition()
        artifact = {
            "activities": [
                {"change_type": "MODIFY", "id": "activity_invented", "name": "인수인계", "role": "담당자"},
            ]
        }

        merged_once, demoted_once = merge_process_definition_artifact_into_definition(definition, artifact)
        merged_twice, demoted_twice = merge_process_definition_artifact_into_definition(merged_once, artifact)

        # 두 번째 병합에서도 같은 이름으로 강등된 항목이 이미 있으므로 한 번만 존재해야 한다
        matching = [a for a in merged_twice["activities"] if a["name"] == "인수인계"]
        assert len(matching) == 1
        assert demoted_twice == 1  # artifact 자체엔 여전히 MODIFY로 남아있어 두 번째 병합에서도 강등으로 집계됨

    def test_sequence_add_maps_from_to_to_source_target(self):
        """artifact의 sequences는 id/name 없이 from/to만 준다 — live 스키마(source/target)로
        옮겨 담고, from/to 조합으로 id를 생성해야 한다(이름 기준으로 생성하면 sequences는
        전부 name이 없어 서로 충돌해 두 번째부터 누락되는 버그가 있었다)."""
        definition = _base_definition()
        artifact = {
            "sequences": [
                {"change_type": "ADD", "from": "activity_review", "to": "activity_approve", "condition": ""},
                {"change_type": "ADD", "from": "activity_approve", "to": "end_event", "condition": ""},
            ]
        }

        merged, demoted = merge_process_definition_artifact_into_definition(definition, artifact)

        assert len(merged["sequences"]) == 2
        assert demoted == 0
        first, second = merged["sequences"]
        assert first["source"] == "activity_review" and first["target"] == "activity_approve"
        assert second["source"] == "activity_approve" and second["target"] == "end_event"
        assert "from" not in first and "to" not in first
        assert first["id"] != second["id"]

    def test_sequence_modify_has_no_id_so_always_demoted(self):
        definition = _base_definition()
        artifact = {
            "sequences": [
                {"change_type": "MODIFY", "from": "activity_review", "to": "activity_approve", "condition": "금액 > 100"},
            ]
        }

        merged, demoted = merge_process_definition_artifact_into_definition(definition, artifact)

        assert demoted == 1
        assert len(merged["sequences"]) == 1
        assert merged["sequences"][0]["source"] == "activity_review"

    def test_original_definition_is_not_mutated(self):
        definition = _base_definition()
        artifact = {
            "activities": [
                {"change_type": "ADD", "id": "activity_approve", "name": "승인", "role": "임원"},
            ]
        }

        merge_process_definition_artifact_into_definition(definition, artifact)

        assert len(definition["activities"]) == 1
