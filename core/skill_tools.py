"""
Deep Agent용 스킬 도구 정의
피드백 기반 스킬 개선에 필요한 도구만 제공 (HTTP API 전용)
"""

import json
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from utils.logger import log, handle_error
from core.skill_api_client import (
    check_skill_exists_with_info,
    get_skill_file_content,
    get_skill_files,
    list_uploaded_skills,
)


def _parse_comma_separated_list(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return [s.strip() for s in text.split(",") if s.strip()]


def _parse_skill_ids_input(skill_ids: Any) -> List[str]:
    raw = skill_ids
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("skill_ids", "") or ""
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                raw = obj.get("skill_ids", "") or ""
            elif isinstance(obj, list):
                raw = ",".join(str(x) for x in obj)
        except (json.JSONDecodeError, TypeError):
            pass
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def create_skill_tools(
    agent_id: Optional[str] = None,
    feedback_content: Optional[str] = None,
    activity_ref: Optional[Dict[str, str]] = None,
) -> list:
    """agent_id 또는 activity_ref가 바인딩된 스킬 도구 목록 생성.

    agent_id가 있으면 기존 에이전트 귀속 동작을 그대로 사용한다. agent_id가 없으면
    activity_ref({"tenant_id", "proc_def_id", "activity_id"})로 지정된 프로세스 활동에
    스킬을 귀속시키는 활동 전용 도구 세트를 반환한다.
    """

    @tool
    async def search_similar_skills(content: str, threshold: float = 0.7) -> str:
        """
        피드백 내용과 유사한 기존 스킬을 검색합니다.
        HTTP API를 통해 업로드된 스킬 목록을 조회합니다.

        Args:
            content: 검색할 내용 (피드백 내용 또는 키워드)
            threshold: 유사도 임계값 (0.0-1.0)
        """
        try:
            from core.database import (
                _get_agent_by_id,
                register_knowledge,
                update_knowledge_access_time,
                load_activity_skills,
            )

            if agent_id:
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else None
                bound_names = _parse_comma_separated_list(agent_info.get("skills") if agent_info else None)
            else:
                ref = activity_ref or {}
                tenant_id = ref.get("tenant_id")
                bound_names = load_activity_skills(
                    tenant_id=ref.get("tenant_id", ""),
                    proc_def_id=ref.get("proc_def_id", ""),
                    activity_id=ref.get("activity_id", ""),
                )

            results: List[Dict] = []
            skill_names_found: set = set()

            # HTTP API로 업로드된 스킬 목록 조회
            uploaded_skills_set: set = set()
            try:
                uploaded_skills = list_uploaded_skills(tenant_id or "")
                uploaded_skills_set = {s.get("name", "") for s in uploaded_skills if s.get("name")}
            except Exception:
                pass

            # 귀속 대상(에이전트 또는 활동)에 이미 있는 스킬도 확인
            for sn in bound_names:
                if sn not in uploaded_skills_set:
                    try:
                        info = check_skill_exists_with_info(sn, tenant_id or "")
                        if info and info.get("exists"):
                            uploaded_skills_set.add(sn)
                    except Exception:
                        pass

            # 각 스킬의 상세 정보 조회
            for sn in uploaded_skills_set:
                if sn in skill_names_found:
                    continue
                try:
                    info = check_skill_exists_with_info(sn, tenant_id or "")
                    if info and info.get("exists"):
                        results.append({
                            "id": sn,
                            "name": info.get("name", sn),
                            "description": info.get("description", ""),
                            "verified": True,
                        })
                        skill_names_found.add(sn)
                except Exception:
                    pass

            if not results:
                return f"관련된 기존 스킬이 없습니다. (검색 임계값: {threshold})\n새 스킬을 생성할지 판단하세요."

            # 레지스트리 등록 — agent_knowledge_registry.agent_id는 NOT NULL이라 활동 전용
            # 경로(agent_id 없음)에서는 등록을 건너뛴다.
            for item in (results if agent_id else []):
                try:
                    register_knowledge(
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        knowledge_type="SKILL",
                        knowledge_id=item.get("id", ""),
                        knowledge_name=item.get("name", ""),
                        content_summary=item.get("description", ""),
                    )
                    update_knowledge_access_time(agent_id, "SKILL", item.get("id", ""))
                except Exception:
                    pass

            output_lines = [f"총 {len(results)}개의 스킬을 찾았습니다:\n"]
            for idx, item in enumerate(results[:15], start=1):
                sid = item.get("id", "")
                sname = item.get("name", sid)
                desc = item.get("description", "")
                output_lines.append(f"[{idx}] 이름: {sname}")
                if sid != sname:
                    output_lines.append(f"    ID: {sid}")
                if desc:
                    output_lines.append(f"    설명: {desc[:200]}")
                output_lines.append("")

            output_lines.append("━" * 50)
            output_lines.append("위 스킬 목록을 바탕으로 판단하세요:")
            output_lines.append("- 기존 스킬로 충분하면 attach_skills_to_agent 사용")
            output_lines.append("- 수정이 필요하면 commit_to_skill(operation=UPDATE)")
            output_lines.append("- 새 절차가 필요하면 commit_to_skill(operation=CREATE)")
            output_lines.append("- 상세 내용이 필요하면 get_skill_detail로 조회")
            return "\n".join(output_lines)

        except Exception as e:
            handle_error("search_similar_skills", e)
            return f"❌ 스킬 검색 실패: {str(e)}"

    @tool
    async def get_skill_detail(skill_name: str) -> str:
        """
        기존 스킬의 전체 상세 내용을 조회합니다.
        SKILL.md 및 부가 파일들의 내용을 모두 반환합니다.

        Args:
            skill_name: 조회할 스킬 이름/ID
        """
        try:
            output_lines = [f"📄 스킬 상세 조회: {skill_name}\n"]

            if agent_id:
                from core.database import _get_agent_by_id
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else ""
            else:
                tenant_id = (activity_ref or {}).get("tenant_id", "")

            # HTTP API 조회
            try:
                info = check_skill_exists_with_info(skill_name, tenant_id or "")
                if not info or not info.get("exists"):
                    return f"❌ 스킬을 찾을 수 없습니다: {skill_name}"

                file_info = get_skill_file_content(skill_name, "SKILL.md", tenant_id or "")
                output_lines.append(f"이름: {info.get('name', skill_name)}")
                if info.get("description"):
                    output_lines.append(f"설명: {info['description']}")

                content = file_info.get("content", "")
                if content:
                    output_lines.append(f"\n📜 SKILL.md 내용:")
                    output_lines.append("```markdown")
                    output_lines.append(content)
                    output_lines.append("```")

            except Exception as e:
                return f"❌ 스킬 조회 실패: {str(e)}"

            # 부가 파일 조회
            try:
                files = get_skill_files(skill_name, tenant_id or "")
                if files:
                    output_lines.append(f"\n📁 부가 파일 ({len(files)}개):")
                    for fi in files:
                        fp = fi.get("path", "")
                        fs = fi.get("size", 0)
                        try:
                            fc_info = get_skill_file_content(skill_name, fp, tenant_id or "")
                            if fc_info.get("type") == "text" and fc_info.get("content"):
                                ext = fp.split(".")[-1].lower() if "." in fp else "text"
                                output_lines.append(f"\n📄 {fp} ({fs} bytes):")
                                output_lines.append(f"```{ext}")
                                output_lines.append(fc_info["content"])
                                output_lines.append("```")
                            else:
                                output_lines.append(f"📄 {fp} ({fs} bytes, binary)")
                        except Exception:
                            output_lines.append(f"📄 {fp} ({fs} bytes, 조회 실패)")
            except Exception:
                pass

            return "\n".join(output_lines)

        except Exception as e:
            handle_error("get_skill_detail", e)
            return f"❌ 스킬 상세 조회 실패: {str(e)}"

    @tool
    async def commit_to_skill(
        operation: str = "CREATE",
        skill_id: Optional[str] = None,
        skill_name: Optional[str] = None,
        description: Optional[str] = None,
        body_markdown: Optional[str] = None,
        additional_files: Optional[str] = None,
    ) -> str:
        """
        스킬을 생성/수정/삭제합니다.
        CREATE/UPDATE 시 SKILL.md 내용(skill_name, description, body_markdown)을
        skill-creator의 작성 가이드(frontmatter + 섹션 구성)를 참고해 직접 채워서 전달하세요.

        Args:
            operation: 작업 타입 (CREATE | UPDATE | DELETE)
            skill_id: UPDATE/DELETE 시 기존 스킬 이름 (필수). CREATE 시 비워둠.
            skill_name: CREATE 시 새 스킬 이름(frontmatter name, 필수). UPDATE/DELETE는 skill_id를 이름으로 사용하므로 비워둠.
            description: 스킬 설명(frontmatter description) — 언제 사용해야 하는지 포함
            body_markdown: SKILL.md 본문 전체(개요/단계별 절차 등, frontmatter 제외). CREATE/UPDATE 시 필수.
            additional_files: 부가 파일 JSON 객체 문자열, 예: '{"scripts/run.py": "..."}' (선택)
        """
        try:
            from core.learning_committers import commit_to_skill as _commit

            if operation == "DELETE" and not (skill_id and str(skill_id).strip()):
                return "❌ DELETE에는 skill_id(기존 스킬 이름)가 필요합니다."
            if operation == "UPDATE" and not (skill_id and str(skill_id).strip()):
                return "❌ UPDATE에는 skill_id(기존 스킬 이름)가 필요합니다."
            if operation == "CREATE" and not (feedback_content and str(feedback_content).strip()):
                return "❌ CREATE에는 피드백이 필요합니다."

            skill_artifact = None
            if operation in ("CREATE", "UPDATE"):
                if not (body_markdown and str(body_markdown).strip()):
                    return "❌ CREATE/UPDATE에는 body_markdown(SKILL.md 본문)이 필요합니다."
                if operation == "CREATE" and not (skill_name and str(skill_name).strip()):
                    return "❌ CREATE에는 skill_name(새 스킬 이름)이 필요합니다."

                parsed_files: Optional[Dict[str, str]] = None
                if additional_files:
                    try:
                        obj = json.loads(additional_files)
                        if isinstance(obj, dict):
                            parsed_files = obj
                        else:
                            return "❌ additional_files는 JSON 객체 문자열이어야 합니다."
                    except (json.JSONDecodeError, TypeError):
                        return "❌ additional_files는 JSON 객체 문자열이어야 합니다."

                skill_artifact = {
                    "name": skill_name,
                    "description": description,
                    "body_markdown": body_markdown,
                    "additional_files": parsed_files or {},
                }

            await _commit(
                agent_id=agent_id,
                skill_artifact=skill_artifact,
                operation=operation,
                skill_id=skill_id,
                feedback_content=feedback_content or "",
                tenant_id=(activity_ref or {}).get("tenant_id") if not agent_id else None,
                activity_ref=activity_ref if not agent_id else None,
            )

            owner_label = f"에이전트: {agent_id}" if agent_id else f"활동: {activity_ref}"
            msgs = {
                "CREATE": f"✅ 스킬이 성공적으로 생성되었습니다. ({owner_label})",
                "UPDATE": f"✅ 스킬이 성공적으로 수정되었습니다. (ID: {skill_id}, {owner_label})",
                "DELETE": f"✅ 스킬이 성공적으로 삭제되었습니다. (ID: {skill_id}, {owner_label})",
            }
            return msgs.get(operation, f"⚠️ 알 수 없는 작업: {operation}")
        except Exception as e:
            handle_error("commit_to_skill", e)
            return f"❌ 스킬 저장 실패: {str(e)}"

    @tool
    async def attach_skills_to_agent(skill_ids: str) -> str:
        """
        기존 스킬을 에이전트에 적재합니다. 스킬 내용은 생성/수정하지 않습니다.
        유사도가 높은 기존 스킬로 요구사항을 충족할 때 사용합니다.

        Args:
            skill_ids: 에이전트에 적재할 스킬 이름 (쉼표 구분, 예: 'skill-a, skill-b')
        """
        try:
            from core.database import (
                _get_agent_by_id,
                update_agent_and_tenant_skills,
                register_knowledge,
                record_knowledge_history,
            )

            skill_names = _parse_skill_ids_input(skill_ids)
            if not skill_names:
                return "❌ skill_ids가 비어있습니다."

            agent_info = _get_agent_by_id(agent_id)
            if not agent_info:
                return f"❌ 에이전트를 찾을 수 없습니다: {agent_id}"
            tenant_id = agent_info.get("tenant_id")

            attached = []
            for sn in skill_names[:10]:
                try:
                    update_agent_and_tenant_skills(agent_id, sn, "CREATE")
                    register_knowledge(
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        knowledge_type="SKILL",
                        knowledge_id=sn,
                        knowledge_name=sn,
                        content_summary=f"기존 스킬 적재: {sn}",
                    )
                    record_knowledge_history(
                        knowledge_type="SKILL",
                        knowledge_id=sn,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        operation="CREATE",
                        new_content={"source": "attach_existing_skill", "skill_name": sn},
                        feedback_content=None,
                        knowledge_name=sn,
                    )
                    attached.append(sn)
                    log(f"✅ 스킬 적재 완료: {sn} (agent_id={agent_id})")
                except Exception as e:
                    log(f"⚠️ 스킬 적재 실패 ({sn}): {e}")

            if not attached:
                return f"❌ 스킬 적재 실패: {', '.join(skill_names)}"
            return f"✅ 기존 스킬 {len(attached)}개를 에이전트에 적재했습니다: {', '.join(attached)}"
        except Exception as e:
            handle_error("attach_skills_to_agent", e)
            return f"❌ 스킬 적재 실패: {str(e)}"

    @tool
    async def attach_skill_to_activity(skill_ids: str) -> str:
        """
        기존 스킬을 프로세스 활동(activity)에 적재합니다. 담당 에이전트가 없을 때 사용하는
        attach_skills_to_agent의 활동 전용 버전입니다. 스킬 내용은 생성/수정하지 않습니다.

        Args:
            skill_ids: 활동에 적재할 스킬 이름 (쉼표 구분, 예: 'skill-a, skill-b')
        """
        try:
            from core.database import update_activity_skills

            skill_names = _parse_skill_ids_input(skill_ids)
            if not skill_names:
                return "❌ skill_ids가 비어있습니다."
            if not activity_ref:
                return "❌ 귀속시킬 활동 정보(activity_ref)가 없습니다."

            attached = []
            for sn in skill_names[:10]:
                try:
                    ok = update_activity_skills(
                        tenant_id=activity_ref.get("tenant_id", ""),
                        proc_def_id=activity_ref.get("proc_def_id", ""),
                        activity_id=activity_ref.get("activity_id", ""),
                        skill_name=sn,
                        operation="CREATE",
                    )
                    if ok:
                        attached.append(sn)
                        log(f"✅ 스킬 적재 완료: {sn} (activity_ref={activity_ref})")
                    else:
                        log(f"⚠️ 스킬 적재 실패 ({sn}): 활동을 찾을 수 없음")
                except Exception as e:
                    log(f"⚠️ 스킬 적재 실패 ({sn}): {e}")

            if not attached:
                return f"❌ 스킬 적재 실패: {', '.join(skill_names)}"
            return f"✅ 기존 스킬 {len(attached)}개를 활동에 적재했습니다: {', '.join(attached)}"
        except Exception as e:
            handle_error("attach_skill_to_activity", e)
            return f"❌ 스킬 적재 실패: {str(e)}"

    if agent_id:
        return [search_similar_skills, get_skill_detail, commit_to_skill, attach_skills_to_agent]
    return [search_similar_skills, get_skill_detail, commit_to_skill, attach_skill_to_activity]
