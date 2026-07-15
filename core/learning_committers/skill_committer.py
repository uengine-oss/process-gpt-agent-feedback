"""
Skill 커밋 모듈
HTTP API를 통해 스킬을 저장/수정/삭제합니다.
"""

from typing import Dict, List, Optional
from utils.logger import log, handle_error
from core.database import (
    update_agent_and_tenant_skills,
    update_activity_skills,
    _get_agent_by_id,
    record_knowledge_history,
)
from core.skill_api_client import (
    upload_skill,
    update_skill_file,
    delete_skill,
    check_skill_exists,
    get_skill_file_content,
)


def _sync_skill_attribution(
    agent_id: Optional[str],
    activity_ref: Optional[Dict[str, str]],
    skill_name: str,
    operation: str,
) -> None:
    """스킬 생성/삭제 후 귀속 대상에 동기화. agent_id가 있으면 에이전트/테넌트,
    없으면 activity_ref로 지정된 프로세스 활동에 반영한다. 실패는 무시."""
    try:
        if agent_id:
            update_agent_and_tenant_skills(agent_id, skill_name, operation)
        elif activity_ref:
            update_activity_skills(
                tenant_id=activity_ref.get("tenant_id", ""),
                proc_def_id=activity_ref.get("proc_def_id", ""),
                activity_id=activity_ref.get("activity_id", ""),
                skill_name=skill_name,
                operation=operation,
            )
    except Exception as e:
        log(f"   ⚠️ 스킬 동기화 실패 (무시): {e}")


def _record_skill_history(agent_id: Optional[str], **kwargs) -> None:
    """agent_knowledge_history.agent_id는 NOT NULL이므로 agent_id가 없는(활동 전용)
    경로에서는 이력을 남기지 않는다 — 귀속은 proc_def.definition 쪽에만 기록된다."""
    if not agent_id:
        return
    try:
        record_knowledge_history(agent_id=agent_id, **kwargs)
    except Exception as e:
        log(f"   ⚠️ 변경 이력 기록 실패 (무시): {e}")


async def commit_to_skill(
    agent_id: Optional[str] = None,
    skill_artifact: Optional[Dict] = None,
    operation: str = "CREATE",
    skill_id: str = None,
    feedback_content: Optional[str] = None,
    merge_mode: Optional[str] = None,
    relationship_analysis: Optional[str] = None,
    related_skill_ids: Optional[str] = None,
    tenant_id: Optional[str] = None,
    activity_ref: Optional[Dict[str, str]] = None,
):
    """
    Skill CRUD 작업 수행 (HTTP API 경로).

    Args:
        agent_id: 에이전트 ID. None이면 담당 에이전트가 없는 배치용 경로로 동작하며,
            tenant_id/activity_ref가 대신 필요하다.
        skill_artifact: Skill 정보 (name, steps, description, overview, usage, additional_files)
        operation: "CREATE" | "UPDATE" | "DELETE"
        skill_id: UPDATE/DELETE 시 기존 스킬 이름
        feedback_content: 원본 피드백
        merge_mode: UPDATE 시 MERGE | REPLACE
        relationship_analysis: 관계 분석 결과
        related_skill_ids: 관련 스킬 이름/ID
        tenant_id: agent_id가 없을 때 스킬을 저장할 테넌트 (배치에서 직접 전달, agent 조회로 유추하지 않음)
        activity_ref: agent_id가 없을 때 스킬을 귀속시킬 활동 {"tenant_id", "proc_def_id", "activity_id"}
    """
    try:
        agent_info = _get_agent_by_id(agent_id) if agent_id else None
        resolved_tenant_id = agent_info.get("tenant_id") if agent_info else tenant_id

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

            log(f"🗑️ SKILL 삭제 시작: 귀속={agent_id or activity_ref}, skill_name={skill_name}")

            previous_content = None
            try:
                if check_skill_exists(skill_name, resolved_tenant_id or ""):
                    try:
                        skill_file_info = get_skill_file_content(skill_name, "SKILL.md", resolved_tenant_id or "")
                        previous_content = skill_file_info.get("content", "")
                    except Exception as e:
                        log(f"   ⚠️ 삭제 전 스킬 내용 조회 실패: {e}")
            except Exception:
                pass

            try:
                if not check_skill_exists(skill_name, resolved_tenant_id or ""):
                    log(f"   ⚠️ 스킬이 존재하지 않습니다: {skill_name}")
                    return
                result = delete_skill(skill_name, resolved_tenant_id or "")
                log(f"   ✅ SKILL 삭제 완료: {result.get('message', 'Success')}")
            except Exception as e:
                log(f"   ❌ SKILL 삭제 실패: {e}")
                raise

            _sync_skill_attribution(agent_id, activity_ref, skill_name, "DELETE")

            _record_skill_history(
                agent_id,
                knowledge_type="SKILL",
                knowledge_id=skill_name,
                tenant_id=resolved_tenant_id,
                operation="DELETE",
                previous_content=previous_content,
                feedback_content=feedback_content,
                knowledge_name=skill_name,
            )

        if operation == "UPDATE":
            if not skill_name:
                raise ValueError("UPDATE 작업에는 skill_id(스킬 이름)가 필요합니다")

            log(f"✏️ SKILL 수정 시작: 귀속={agent_id or activity_ref}, skill_name={skill_name}")

            previous_content = None
            try:
                if check_skill_exists(skill_name, resolved_tenant_id or ""):
                    try:
                        skill_file_info = get_skill_file_content(skill_name, "SKILL.md", resolved_tenant_id or "")
                        previous_content = skill_file_info.get("content", "")
                    except Exception as e:
                        log(f"   ⚠️ 업데이트 전 스킬 내용 조회 실패: {e}")
            except Exception:
                pass

            try:
                if not check_skill_exists(skill_name, resolved_tenant_id or ""):
                    log(f"   ⚠️ 스킬이 존재하지 않습니다. CREATE로 전환: {skill_name}")
                    operation = "CREATE"
                else:
                    skill_document = _format_skill_document(
                        skill_name, steps, description=description, overview=overview, usage=usage, body_markdown=body_markdown
                    )
                    new_content = skill_document

                    result = update_skill_file(skill_name, "SKILL.md", skill_document, resolved_tenant_id or "")
                    log(f"   ✅ SKILL.md 업데이트 완료: {result.get('message', 'Success')}")

                    if additional_files:
                        for file_path, file_content in additional_files.items():
                            try:
                                update_skill_file(skill_name, file_path, file_content, resolved_tenant_id or "")
                                log(f"   ✅ 파일 업데이트 완료: {file_path}")
                            except Exception as e:
                                log(f"   ⚠️ 파일 업데이트 실패 ({file_path}): {e}")

                    log(f"   ✅ SKILL 수정 완료: skill_name={skill_name}")

                    _record_skill_history(
                        agent_id,
                        knowledge_type="SKILL",
                        knowledge_id=skill_name,
                        tenant_id=resolved_tenant_id,
                        operation="UPDATE",
                        previous_content=previous_content,
                        new_content=new_content,
                        feedback_content=feedback_content,
                        knowledge_name=skill_name,
                    )

                    return

            except Exception as e:
                log(f"   ⚠️ 스킬 수정 실패, CREATE로 전환: {e}")
                operation = "CREATE"

        if operation == "CREATE":
            log(f"✅ SKILL 저장 시작: 귀속={agent_id or activity_ref}, skill_name={skill_name}")

            try:
                if agent_id and not agent_info:
                    raise ValueError(f"에이전트를 찾을 수 없습니다: agent_id={agent_id}")
                if not resolved_tenant_id:
                    raise ValueError(
                        f"tenant_id를 확인할 수 없습니다: agent_id={agent_id}, tenant_id={tenant_id}"
                    )
                if not agent_id and not activity_ref:
                    raise ValueError("agent_id가 없으면 activity_ref(귀속 대상 활동)가 필요합니다.")

                if check_skill_exists(skill_name, resolved_tenant_id or ""):
                    raise ValueError(f"스킬 '{skill_name}'이(가) 이미 존재합니다. 수정하려면 UPDATE를 사용하세요.")

                skill_document = _format_skill_document(
                    skill_name, steps, description=description, overview=overview, usage=usage, body_markdown=body_markdown
                )
                new_content = skill_document

                result = upload_skill(
                    skill_name=skill_name,
                    skill_content=skill_document,
                    tenant_id=resolved_tenant_id,
                    additional_files=additional_files if additional_files else None,
                )

                log(f"   ✅ SKILL 저장 완료: skill_name={skill_name}")

                _sync_skill_attribution(agent_id, activity_ref, skill_name, "CREATE")

                _record_skill_history(
                    agent_id,
                    knowledge_type="SKILL",
                    knowledge_id=skill_name,
                    tenant_id=resolved_tenant_id,
                    operation="CREATE",
                    new_content=new_content,
                    feedback_content=feedback_content,
                    knowledge_name=skill_name,
                )

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
