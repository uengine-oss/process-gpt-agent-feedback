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


def create_skill_tools(agent_id: str, feedback_content: Optional[str] = None) -> list:
    """agent_id가 바인딩된 스킬 도구 목록 생성"""

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
            from core.database import _get_agent_by_id, register_knowledge, update_knowledge_access_time

            agent_info = _get_agent_by_id(agent_id)
            tenant_id = agent_info.get("tenant_id") if agent_info else None
            agent_skills = agent_info.get("skills") if agent_info else None

            results: List[Dict] = []
            skill_names_found: set = set()

            # HTTP API로 업로드된 스킬 목록 조회
            uploaded_skills_set: set = set()
            try:
                uploaded_skills = list_uploaded_skills()
                uploaded_skills_set = {s.get("name", "") for s in uploaded_skills if s.get("name")}
            except Exception:
                pass

            # agent_skills에 있는 스킬도 확인
            if agent_skills:
                for sn in [s.strip() for s in agent_skills.split(",") if s.strip()]:
                    if sn not in uploaded_skills_set:
                        try:
                            info = check_skill_exists_with_info(sn)
                            if info and info.get("exists"):
                                uploaded_skills_set.add(sn)
                        except Exception:
                            pass

            # 각 스킬의 상세 정보 조회
            for sn in uploaded_skills_set:
                if sn in skill_names_found:
                    continue
                try:
                    info = check_skill_exists_with_info(sn)
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

            # 레지스트리 등록
            for item in results:
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

            # HTTP API 조회
            try:
                info = check_skill_exists_with_info(skill_name)
                if not info or not info.get("exists"):
                    return f"❌ 스킬을 찾을 수 없습니다: {skill_name}"

                file_info = get_skill_file_content(skill_name, "SKILL.md")
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
                files = get_skill_files(skill_name)
                if files:
                    output_lines.append(f"\n📁 부가 파일 ({len(files)}개):")
                    for fi in files:
                        fp = fi.get("path", "")
                        fs = fi.get("size", 0)
                        try:
                            fc_info = get_skill_file_content(skill_name, fp)
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
    ) -> str:
        """
        스킬을 생성/수정/삭제합니다.
        스킬 내용(SKILL.md, steps, additional_files)은 skill-creator가 생성합니다.

        Args:
            operation: 작업 타입 (CREATE | UPDATE | DELETE)
            skill_id: UPDATE/DELETE 시 기존 스킬 이름 (필수). CREATE 시 비워둠.
        """
        try:
            from core.learning_committers import commit_to_skill as _commit

            if operation == "DELETE" and not (skill_id and str(skill_id).strip()):
                return "❌ DELETE에는 skill_id(기존 스킬 이름)가 필요합니다."
            if operation == "UPDATE" and not (skill_id and str(skill_id).strip()):
                return "❌ UPDATE에는 skill_id(기존 스킬 이름)가 필요합니다."
            if operation == "CREATE" and not (feedback_content and str(feedback_content).strip()):
                return "❌ CREATE에는 피드백이 필요합니다."

            await _commit(
                agent_id=agent_id,
                skill_artifact=None,
                operation=operation,
                skill_id=skill_id,
                feedback_content=feedback_content or "",
            )

            msgs = {
                "CREATE": f"✅ 스킬이 성공적으로 생성되었습니다. (에이전트: {agent_id})",
                "UPDATE": f"✅ 스킬이 성공적으로 수정되었습니다. (ID: {skill_id}, 에이전트: {agent_id})",
                "DELETE": f"✅ 스킬이 성공적으로 삭제되었습니다. (ID: {skill_id}, 에이전트: {agent_id})",
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

    return [search_similar_skills, get_skill_detail, commit_to_skill, attach_skills_to_agent]
