"""
Skill 커밋 모듈
HTTP API를 통해 스킬을 저장/수정/삭제합니다.
"""

from typing import Dict, List, Optional
from utils.logger import log, handle_error
from core.database import update_agent_and_tenant_skills, _get_agent_by_id, record_knowledge_history
from core.skill_api_client import (
    upload_skill,
    update_skill_file,
    delete_skill,
    check_skill_exists,
    get_skill_file_content,
)


async def commit_to_skill(
    agent_id: str,
    skill_artifact: Optional[Dict] = None,
    operation: str = "CREATE",
    skill_id: str = None,
    feedback_content: Optional[str] = None,
    merge_mode: Optional[str] = None,
    relationship_analysis: Optional[str] = None,
    related_skill_ids: Optional[str] = None,
):
    """
    Skill CRUD 작업 수행 (HTTP API 경로).

    Args:
        agent_id: 에이전트 ID
        skill_artifact: Skill 정보 (name, steps, description, overview, usage, additional_files)
        operation: "CREATE" | "UPDATE" | "DELETE"
        skill_id: UPDATE/DELETE 시 기존 스킬 이름
        feedback_content: 원본 피드백
        merge_mode: UPDATE 시 MERGE | REPLACE
        relationship_analysis: 관계 분석 결과
        related_skill_ids: 관련 스킬 이름/ID
    """
    try:
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None

        if operation in ("CREATE", "UPDATE") and skill_artifact is None:
            raise ValueError("CREATE/UPDATE 시 skill_artifact가 필요합니다.")

        skill_name = skill_id or (skill_artifact.get("name", "피드백 기반 스킬") if skill_artifact else None)
        steps = (skill_artifact or {}).get("steps", [])
        additional_files = (skill_artifact or {}).get("additional_files", {})
        description = (skill_artifact or {}).get("description", f"{skill_name or '스킬'} 작업을 수행하기 위한 단계별 절차입니다.")
        overview = (skill_artifact or {}).get("overview")
        usage = (skill_artifact or {}).get("usage")
        body_markdown = (skill_artifact or {}).get("body_markdown")

        if operation == "DELETE":
            if not skill_name:
                raise ValueError("DELETE 작업에는 skill_id(스킬 이름)가 필요합니다")

            log(f"🗑️ SKILL 삭제 시작: 에이전트 {agent_id}, skill_name={skill_name}")

            previous_content = None
            try:
                if check_skill_exists(skill_name):
                    try:
                        skill_file_info = get_skill_file_content(skill_name, "SKILL.md")
                        previous_content = skill_file_info.get("content", "")
                    except Exception as e:
                        log(f"   ⚠️ 삭제 전 스킬 내용 조회 실패: {e}")
            except Exception:
                pass

            try:
                if not check_skill_exists(skill_name):
                    log(f"   ⚠️ 스킬이 존재하지 않습니다: {skill_name}")
                    return
                result = delete_skill(skill_name)
                log(f"   ✅ SKILL 삭제 완료: {result.get('message', 'Success')}")
            except Exception as e:
                log(f"   ❌ SKILL 삭제 실패: {e}")
                raise

            try:
                update_agent_and_tenant_skills(agent_id, skill_name, "DELETE")
            except Exception as e:
                log(f"   ⚠️ 스킬 동기화 실패 (무시): {e}")

            try:
                record_knowledge_history(
                    knowledge_type="SKILL",
                    knowledge_id=skill_name,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="DELETE",
                    previous_content=previous_content,
                    feedback_content=feedback_content,
                    knowledge_name=skill_name,
                )
            except Exception as e:
                log(f"   ⚠️ 변경 이력 기록 실패 (무시): {e}")

        if operation == "UPDATE":
            if not skill_name:
                raise ValueError("UPDATE 작업에는 skill_id(스킬 이름)가 필요합니다")

            log(f"✏️ SKILL 수정 시작: 에이전트 {agent_id}, skill_name={skill_name}")

            previous_content = None
            try:
                if check_skill_exists(skill_name):
                    try:
                        skill_file_info = get_skill_file_content(skill_name, "SKILL.md")
                        previous_content = skill_file_info.get("content", "")
                    except Exception as e:
                        log(f"   ⚠️ 업데이트 전 스킬 내용 조회 실패: {e}")
            except Exception:
                pass

            try:
                if not check_skill_exists(skill_name):
                    log(f"   ⚠️ 스킬이 존재하지 않습니다. CREATE로 전환: {skill_name}")
                    operation = "CREATE"
                else:
                    skill_document = _format_skill_document(
                        skill_name, steps, description=description, overview=overview, usage=usage, body_markdown=body_markdown
                    )
                    new_content = skill_document

                    result = update_skill_file(skill_name, "SKILL.md", content=skill_document)
                    log(f"   ✅ SKILL.md 업데이트 완료: {result.get('message', 'Success')}")

                    if additional_files:
                        for file_path, file_content in additional_files.items():
                            try:
                                update_skill_file(skill_name, file_path, content=file_content)
                                log(f"   ✅ 파일 업데이트 완료: {file_path}")
                            except Exception as e:
                                log(f"   ⚠️ 파일 업데이트 실패 ({file_path}): {e}")

                    log(f"   ✅ SKILL 수정 완료: skill_name={skill_name}")

                    try:
                        record_knowledge_history(
                            knowledge_type="SKILL",
                            knowledge_id=skill_name,
                            agent_id=agent_id,
                            tenant_id=tenant_id,
                            operation="UPDATE",
                            previous_content=previous_content,
                            new_content=new_content,
                            feedback_content=feedback_content,
                            knowledge_name=skill_name,
                        )
                    except Exception as e:
                        log(f"   ⚠️ 변경 이력 기록 실패 (무시): {e}")

                    return

            except Exception as e:
                log(f"   ⚠️ 스킬 수정 실패, CREATE로 전환: {e}")
                operation = "CREATE"

        if operation == "CREATE":
            log(f"✅ SKILL 저장 시작: 에이전트 {agent_id}, skill_name={skill_name}")

            try:
                if not agent_info:
                    raise ValueError(f"에이전트를 찾을 수 없습니다: agent_id={agent_id}")
                if not tenant_id:
                    raise ValueError(f"에이전트의 tenant_id가 없습니다: agent_id={agent_id}")

                if check_skill_exists(skill_name):
                    raise ValueError(f"스킬 '{skill_name}'이(가) 이미 존재합니다. 수정하려면 UPDATE를 사용하세요.")

                skill_document = _format_skill_document(
                    skill_name, steps, description=description, overview=overview, usage=usage, body_markdown=body_markdown
                )
                new_content = skill_document

                result = upload_skill(
                    skill_name=skill_name,
                    skill_content=skill_document,
                    tenant_id=tenant_id,
                    additional_files=additional_files if additional_files else None,
                )

                log(f"   ✅ SKILL 저장 완료: skill_name={skill_name}")

                try:
                    update_agent_and_tenant_skills(agent_id, skill_name, "CREATE")
                except Exception as e:
                    log(f"   ⚠️ 스킬 동기화 실패 (무시): {e}")

                try:
                    record_knowledge_history(
                        knowledge_type="SKILL",
                        knowledge_id=skill_name,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        operation="CREATE",
                        new_content=new_content,
                        feedback_content=feedback_content,
                        knowledge_name=skill_name,
                    )
                except Exception as e:
                    log(f"   ⚠️ 변경 이력 기록 실패 (무시): {e}")

            except Exception as e:
                log(f"   ❌ SKILL 저장 실패: {e}")
                raise

    except Exception as e:
        handle_error(f"SKILL{operation}", e)
        raise


def _format_skill_document(
    skill_name: str,
    steps: List[str],
    description: Optional[str] = None,
    overview: Optional[str] = None,
    usage: Optional[str] = None,
    body_markdown: Optional[str] = None,
) -> str:
    if description is None:
        description = f"{skill_name} 작업을 수행하기 위한 단계별 절차입니다."

    lines: List[str] = []
    lines.append("---\n")
    lines.append(f"name: {skill_name}\n")
    lines.append(f"description: {description}\n")
    lines.append("---\n")
    lines.append("\n")

    if body_markdown and body_markdown.strip():
        body = body_markdown.strip()
        if not body.endswith("\n"):
            body += "\n"
        lines.append(body)
        return "".join(lines)

    if overview is None:
        overview = description
    lines.append(f"# {skill_name}\n")
    lines.append("\n")
    lines.append("## 개요\n")
    lines.append(f"{overview}\n")
    lines.append("\n")
    if steps:
        lines.append("## 단계별 실행 절차\n")
        lines.append("\n")
        for idx, step in enumerate(steps, start=1):
            lines.append(f"{idx}. {step}\n")
        lines.append("\n")
    if usage:
        lines.append("## 사용법\n")
        lines.append("\n")
        lines.append(f"{usage}\n")
        lines.append("\n")
    return "".join(lines)
