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
)
from core.skill_api_client import (
    update_skill_file,
    delete_skill,
    check_skill_exists,
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


async def commit_to_skill(
    agent_id: Optional[str] = None,
    skill_artifact: Optional[Dict] = None,
    operation: str = "CREATE",
    skill_id: str = None,
    merge_mode: Optional[str] = None,
    relationship_analysis: Optional[str] = None,
    related_skill_ids: Optional[str] = None,
    tenant_id: Optional[str] = None,
    activity_ref: Optional[Dict[str, str]] = None,
    requester_ids: Optional[List[str]] = None,
    reviewer_id: Optional[str] = None,
):
    """
    Skill CRUD 작업 수행 (HTTP API 경로).

    Args:
        agent_id: 에이전트 ID. None이면 담당 에이전트가 없는 배치용 경로로 동작하며,
            tenant_id/activity_ref가 대신 필요하다.
        skill_artifact: Skill 정보 (name, steps, description, overview, usage, additional_files)
        operation: "CREATE" | "UPDATE" | "DELETE" — CREATE는 지원하지 않는다(피드백 기반
            기존 스킬 개선만 다루는 시스템 정책). UPDATE 대상이 존재하지 않을 때도 내부적으로
            operation이 "CREATE"로 바뀌지만, 이 경우도 포함해 아무 것도 하지 않고 건너뛴다.
        skill_id: UPDATE/DELETE 시 기존 스킬 이름
        merge_mode: UPDATE 시 MERGE | REPLACE
        relationship_analysis: 관계 분석 결과
        related_skill_ids: 관련 스킬 이름/ID
        tenant_id: agent_id가 없을 때 스킬을 저장할 테넌트 (배치에서 직접 전달, agent 조회로 유추하지 않음)
        activity_ref: agent_id가 없을 때 스킬을 귀속시킬 활동 {"tenant_id", "proc_def_id", "activity_id"}
        requester_ids: UPDATE 시 열리는 스킬 병합 요청의 requester(피드백 작성자 user_id
            목록, 중복 제거)(fix-merge-request-requester).
        reviewer_id: UPDATE 시 열리는 스킬 병합 요청의 reviewer(승인자).
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

        if operation == "UPDATE":
            if not skill_name:
                raise ValueError("UPDATE 작업에는 skill_id(스킬 이름)가 필요합니다")

            log(f"✏️ SKILL 수정 시작: 귀속={agent_id or activity_ref}, skill_name={skill_name}")

            try:
                if not check_skill_exists(skill_name, resolved_tenant_id or ""):
                    log(f"   ⚠️ 스킬이 존재하지 않습니다. 생성 미지원 — 건너뜀: {skill_name}")
                    operation = "CREATE"
                else:
                    skill_document = _format_skill_document(
                        skill_name, steps, description=description, overview=overview, usage=usage, body_markdown=body_markdown
                    )

                    result = update_skill_file(
                        skill_name,
                        "SKILL.md",
                        skill_document,
                        resolved_tenant_id or "",
                        requester_ids=requester_ids,
                        reviewer_id=reviewer_id,
                    )
                    log(f"   ✅ SKILL.md 업데이트 완료: {result.get('message', 'Success')}")

                    if additional_files:
                        for file_path, file_content in additional_files.items():
                            try:
                                update_skill_file(
                                    skill_name,
                                    file_path,
                                    file_content,
                                    resolved_tenant_id or "",
                                    requester_ids=requester_ids,
                                    reviewer_id=reviewer_id,
                                )
                                log(f"   ✅ 파일 업데이트 완료: {file_path}")
                            except Exception as e:
                                log(f"   ⚠️ 파일 업데이트 실패 ({file_path}): {e}")

                    log(f"   ✅ SKILL 수정 완료: skill_name={skill_name}")

                    return

            except Exception as e:
                log(f"   ⚠️ 스킬 수정 실패, 생성 미지원 — 건너뜀: {e}")
                operation = "CREATE"

        if operation == "CREATE":
            # 이 시스템은 피드백 기반 기존 스킬 개선만 다룬다 — 신규 생성 경로는 없다.
            # UPDATE 대상이 존재하지 않을 때도(위 두 분기) 여기로 흘러들어오므로, 두
            # 경우 모두 조용히 건너뛴다.
            log(f"⏭️ 스킬 생성 미지원, 건너뜀: 귀속={agent_id or activity_ref}, skill_name={skill_name}")
            return

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
