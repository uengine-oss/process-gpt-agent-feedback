"""
Skill 커밋 모듈
HTTP API를 통해 스킬을 저장/수정/삭제합니다.
"""

from typing import Dict, List, Optional
from utils.logger import log, handle_error
from core.apply_tracking import record_skill_commit
from core.database import (
    update_agent_and_tenant_skills,
    update_activity_skills,
    _get_agent_by_id,
    record_skill_contribution,
    find_resource_pull_request,
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


def _record_skill_contribution(
    tenant_id: Optional[str],
    skill_name: str,
    operation: str,
    contributor_user_ids: Optional[List[str]],
    contribution_source: str,
) -> None:
    """CREATE/UPDATE 에 기여한 사람을 skill_contributions 에 기록한다.

    contributor_user_ids가 없으면(귀속 가능한 사람을 식별할 수 없는 경로) 아무 것도
    기록하지 않는다 — 근거 없는 기여자 추정을 피한다. contribution_source가
    "proposal_approval"이면 피드백 제안 승인으로 반영된 것이므로 PROPOSAL_APPROVED로,
    그 외(에이전트 채팅 등 직접 호출)에는 CREATE/UPDATE 그대로 CREATED/MODIFIED 로 남긴다.
    """
    if not tenant_id or not contributor_user_ids:
        return
    if contribution_source == "proposal_approval":
        contribution_type = "PROPOSAL_APPROVED"
    else:
        contribution_type = "CREATED" if operation == "CREATE" else "MODIFIED"
    for uid in dict.fromkeys(u for u in contributor_user_ids if u):
        try:
            record_skill_contribution(
                tenant_id=tenant_id,
                skill_name=skill_name,
                contributor_user_id=uid,
                contribution_type=contribution_type,
            )
        except Exception as e:
            log(f"   ⚠️ 스킬 기여 이력 기록 실패 (무시): {e}")


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
    contributor_user_ids: Optional[List[str]] = None,
    contribution_source: str = "direct",
):
    """
    Skill CRUD 작업 수행 (HTTP API 경로).

    Args:
        agent_id: 에이전트 ID. None이면 담당 에이전트가 없는 배치용 경로로 동작하며,
            tenant_id/activity_ref가 대신 필요하다.
        skill_artifact: Skill 정보 (name, steps, description, overview, usage, additional_files)
        operation: "CREATE" | "UPDATE" | "DELETE" — CREATE는 지원하지 않는다(피드백 기반
            기존 스킬 개선만 다루는 시스템 정책)로 아무 것도 하지 않고 건너뛴다. UPDATE 대상이
            존재하지 않거나 커밋이 실패하면 예외를 던진다.

    Returns:
        UPDATE면 커밋 결과 dict(branch, pr_created, pull_request_id 등), 그 외에는 None.
        skill_id: UPDATE/DELETE 시 기존 스킬 이름
        merge_mode: UPDATE 시 MERGE | REPLACE
        relationship_analysis: 관계 분석 결과
        related_skill_ids: 관련 스킬 이름/ID
        tenant_id: agent_id가 없을 때 스킬을 저장할 테넌트 (배치에서 직접 전달, agent 조회로 유추하지 않음)
        activity_ref: agent_id가 없을 때 스킬을 귀속시킬 활동 {"tenant_id", "proc_def_id", "activity_id"}
        requester_ids: UPDATE 시 열리는 스킬 병합 요청의 requester(피드백 작성자 user_id
            목록, 중복 제거)(fix-merge-request-requester).
        reviewer_id: UPDATE 시 열리는 스킬 병합 요청의 reviewer(승인자).
        contributor_user_ids: 이 UPDATE 에 기여한 사람(user_id) 목록 — 피드백 원 작성자
            및(제안 승인 경로라면) 승인자. 식별 불가하면 None(기여 이력 미기록).
        contribution_source: "direct"(에이전트 채팅 등 직접 호출) | "proposal_approval"
            (피드백 제안 승인 반영) — skill_contributions.contribution_type 결정에 사용.
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

            # 실패를 조용히 넘기면 안 된다 — 예전에는 대상 스킬이 없거나 커밋이 실패해도
            # "생성 미지원 — 건너뜀"으로 끝나고 도구는 성공 메시지를 돌려줘서, 병합 요청이
            # 하나도 없는데 피드백은 반영 완료(COMPLETED)로 찍혔다.
            try:
                if not check_skill_exists(skill_name, resolved_tenant_id or ""):
                    raise LookupError(f"개선 대상 스킬이 없습니다: {skill_name}")

                skill_document = _format_skill_document(
                    skill_name, steps, description=description, overview=overview, usage=usage, body_markdown=body_markdown
                )
                # 첫 커밋 메시지가 병합 요청 제목이 된다 — 목록에서 피드백 기반 개선임을 알아보게 한다.
                commit_message = f"[Feedback] {skill_name} 스킬 개선"
                result = update_skill_file(
                    skill_name,
                    "SKILL.md",
                    skill_document,
                    resolved_tenant_id or "",
                    requester_ids=requester_ids,
                    reviewer_id=reviewer_id,
                    message=commit_message,
                )
                branch = result.get("branch")
                log(f"   ✅ SKILL.md 커밋 완료: branch={branch}, pr_created={result.get('pr_created')}")

                # 부가 파일은 SKILL.md가 만든 브랜치에 이어서 커밋한다 — 같은 병합 요청에 담긴다.
                failed_files = []
                for file_path, file_content in (additional_files or {}).items():
                    try:
                        update_skill_file(
                            skill_name,
                            file_path,
                            file_content,
                            resolved_tenant_id or "",
                            requester_ids=requester_ids,
                            reviewer_id=reviewer_id,
                            branch=branch,
                            message=f"{commit_message}: {file_path}",
                        )
                        log(f"   ✅ 파일 커밋 완료: {file_path}")
                    except Exception as e:
                        failed_files.append(file_path)
                        log(f"   ⚠️ 파일 커밋 실패 ({file_path}): {e}")
            except Exception as e:
                record_skill_commit({
                    "resource_type": "skill",
                    "resource_id": skill_name,
                    "committed": False,
                    "error": str(e)[:500],
                })
                raise

            pr_row = find_resource_pull_request(resolved_tenant_id or "", "skill", skill_name, branch or "")
            entry = {
                "resource_type": "skill",
                "resource_id": skill_name,
                "committed": True,
                "branch": branch,
                "pr_created": bool(result.get("pr_created")),
                "pull_request_id": (pr_row or {}).get("id"),
                "git_pr_url": result.get("html_url") or (pr_row or {}).get("git_pr_url"),
            }
            if not result.get("pr_created"):
                entry["error"] = result.get("pr_error") or "병합 요청이 만들어지지 않았습니다"
            if failed_files:
                entry["failed_files"] = failed_files
            record_skill_commit(entry)
            log(f"   ✅ SKILL 수정 완료: skill_name={skill_name}, pr_id={entry['pull_request_id']}")

            _record_skill_contribution(
                resolved_tenant_id, skill_name, "UPDATE",
                contributor_user_ids, contribution_source,
            )
            return entry

        if operation == "CREATE":
            # 이 시스템은 피드백 기반 기존 스킬 개선만 다룬다 — 신규 생성 경로는 없다.
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
