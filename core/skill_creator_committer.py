"""
Skill Creator Committer: CREATE/UPDATE 스킬을 computer-use Pod + skill-creator
(init_skill, package_skill, quick_validate)로 생성한 뒤 .skill zip을 파싱하여
skill_api_client로 업로드. base64·zip 실패 시 HTTP 폴백 없이 예외를 전파.
실패해도 의도된 수정 내역(skill_content)이 있으면 record_knowledge_history로
변경 이력에 남긴 뒤 예외를 전파.
"""

import base64
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Any

from utils.logger import log, handle_error
from core.database import (
    _get_agent_by_id,
    update_agent_and_tenant_skills,
    record_knowledge_history,
)
from core.skill_api_client import (
    check_skill_exists,
    get_skill_file_content,
    get_skill_files,
    update_skill_file,
    upload_skill,
)
from core.learning_committers.skill_committer import _format_skill_document
from core.skill_quick_validate import get_quick_validate_script
from core.mcp_client import get_mcp_tool_by_name_async
from core.llm import create_llm


# MCP tool names (claude-skills, computer-use)
_TOOL_READ_SKILL_DOCUMENT = "read_skill_document"
_TOOL_CREATE_SESSION = "create_session"
_TOOL_DELETE_SESSION = "delete_session"
_TOOL_CREATE_FILE = "create_file"
_TOOL_RUN_SHELL = "run_shell"
_TOOL_DELETE_FILE = "delete_file"


def _tool_name_variants(base: str) -> List[str]:
    """MCP 도구 이름 변형 (서버 접두어 등)."""
    return [base, f"mcp_computer-use_{base}", f"mcp_cursor-computer-use_{base}"]


def _extract_text(res: Any) -> str:
    """MCP/LLM 결과에서 문자열 추출. content 블록 리스트, dict, 객체 지원."""
    if res is None:
        return ""
    if isinstance(res, str):
        return res
    if isinstance(res, list):
        return "".join(
            str(x.get("text", ""))
            for x in res
            if isinstance(x, dict) and "text" in x
        )
    if isinstance(res, dict):
        c = res.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(
                str(x.get("text", ""))
                for x in c
                if isinstance(x, dict) and "text" in x
            )
        return res.get("text") or res.get("output") or ""
    if hasattr(res, "content"):
        c = getattr(res, "content", None)
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(
                str(x.get("text", ""))
                for x in c
                if isinstance(x, dict) and "text" in x
            )
        if hasattr(c, "__iter__") and not isinstance(c, (str, bytes)):
            return "".join(str(x) for x in c)
    return str(res)


def _strip_document_header(text: str) -> str:
    """
    claude-skills read_skill_document 응답에서 래퍼를 제거.
    형식: "Document: path\\n\\n========...\\n\\n<실제 스크립트>"
    이 래퍼를 그대로 .py에 쓰면 line 3의 "===..."가 SyntaxError를 유발함.
    """
    if not text or not isinstance(text, str):
        return text or ""
    s = text.lstrip()
    if not s.startswith("Document:"):
        return text
    # "Document: ...\\n\\n=+\\n+" 제거
    m = re.match(r"Document:[^\n]*\n\s*=+\s*\n+", s)
    if m:
        return s[m.end() :].lstrip("\n")
    # 헤더 형식이 다를 수 있으면, 첫 코드 유사 라인(#!, """, import, from)까지 건너뛰기
    for token in ("#!", '"""', "'''", "import ", "from "):
        i = s.find(token)
        if i != -1:
            return s[i:].lstrip()
    return text


def _normalize_and_decode_base64(raw: str) -> bytes:
    """
    MCP run_shell 결과에서 추출한 문자열을 정규화한 뒤 base64 디코딩.
    - 공백/개행 제거, JSON 등에 감싸진 경우 base64 블록 추출, 패딩 보정.
    - 실패 시 BinasciiError 등 그대로 전파.
    """
    # [디코딩 디버그] 입력
    raw_type = type(raw).__name__
    raw_len = len(raw) if raw else 0
    log(f"   [base64] 입력: type={raw_type}, len={raw_len}, 앞150자={repr((raw or '')[:150])}, 뒤150자={repr((raw or '')[-150:])}")

    s = (raw or "").strip()
    if not s:
        log(f"   [base64] strip 후 비어 있음")
        raise ValueError("base64 입력이 비어 있음")
    s = re.sub(r"\s+", "", s)
    log(f"   [base64] 공백 제거 후 len={len(s)}")

    cand = s
    if not re.fullmatch(r"[A-Za-z0-9+/=]*", s):
        parts = re.findall(r"[A-Za-z0-9+/=]{50,}", s)
        if parts:
            cand = max(parts, key=len)
            log(f"   [base64] 비 base64 문자 있음 → 블록 추출: {len(parts)}개 중 최대 len(cand)={len(cand)}")
        else:
            parts = re.findall(r"[A-Za-z0-9+/=]+", s)
            cand = max(parts, key=len) if parts else s
            log(f"   [base64] 50자 이상 블록 없음 → [A-Za-z0-9+/=]+ 조각 {len(parts)}개 중 최대 len(cand)={len(cand)}")
    if not cand:
        log(f"   [base64] base64 추출 블록 없음, s[:200]={repr(s[:200])}")
        raise ValueError("base64에 사용 가능한 블록이 없음")

    pad = (4 - len(cand) % 4) % 4
    cand += "=" * pad
    log(f"   [base64] cand len={len(cand)}, len%4={len(cand) % 4}, pad={pad}, 앞120자={repr(cand[:120])}, 뒤120자={repr(cand[-120:])}")

    try:
        return base64.b64decode(cand)
    except Exception as e1:
        log(f"   [base64] b64decode(cand) 실패: {type(e1).__name__}: {e1}, cand_len={len(cand)}")
        try:
            return base64.b64decode(cand, validate=False)
        except TypeError:
            log(f"   [base64] validate=False 미지원(구버전 파이썬), 원예외 전파")
            raise e1
        except Exception as e2:
            log(f"   [base64] b64decode(validate=False) 도 실패: {type(e2).__name__}: {e2}")
            raise e1


def _extract_session_id(res: Any) -> Optional[str]:
    """create_session 결과에서 session_id 추출.
    MCP computer-use는 {"session_id":"...","pod_name":"..."} 또는
    content 내 text/JSON 문자열로 반환할 수 있음.
    """
    if res is None:
        return None
    # 1) 최상위 dict에 session_id / sessionId
    if isinstance(res, dict):
        sid = res.get("session_id") or res.get("sessionId")
        if sid:
            return str(sid).strip() or None
        # result / data 래핑
        for key in ("result", "data", "response"):
            sub = res.get(key)
            if isinstance(sub, dict):
                sid = sub.get("session_id") or sub.get("sessionId")
                if sid:
                    return str(sid).strip() or None
    # 2) content 문자열(JSON) 파싱
    text = _extract_text(res)
    if text:
        text = (text or "").strip()
        # 이미 JSON 객체 형태의 문자열인 경우
        if text.startswith("{"):
            try:
                data = json.loads(text)
                sid = (data.get("session_id") or data.get("sessionId")) if isinstance(data, dict) else None
                if sid:
                    return str(sid).strip() or None
            except json.JSONDecodeError:
                pass
        # content 블록이 여러 개여서 합쳐진 경우, 마지막 유효 JSON만 시도
        for part in text.replace("}\n{", "}\n").split("\n"):
            part = part.strip()
            if part.startswith("{") and "session_id" in part:
                try:
                    data = json.loads(part)
                    sid = (data.get("session_id") or data.get("sessionId")) if isinstance(data, dict) else None
                    if sid:
                        return str(sid).strip() or None
                except json.JSONDecodeError:
                    pass
    # 3) 객체의 content 속성에서 나온 dict (이미 1에서 처리되지 않은 경우)
    if hasattr(res, "content") and isinstance(getattr(res, "content", None), dict):
        return _extract_session_id(getattr(res, "content"))
    return None


def _log_create_session_debug(cr: Any) -> None:
    """create_session 응답에서 session_id를 못 찾았을 때 디버그 로그."""
    try:
        t = type(cr).__name__
        if isinstance(cr, dict):
            r = {k: (v if k != "content" else ("<len=%s>" % len(v) if isinstance(v, (list, str)) else type(v).__name__)) for k, v in list(cr.items())[:10]}
        else:
            r = repr(cr)[:500]
        log(f"   ⚠️ create_session 응답: type={t}, repr={r}")
        txt = _extract_text(cr)
        if txt:
            log(f"   ⚠️ _extract_text(응답) 길이={len(txt)}, 앞 300자: {repr(txt[:300])}")
    except Exception as e:
        log(f"   ⚠️ create_session 디버그 로그 실패: {e}")


async def _invoke_tool(name: str, **kwargs: Any) -> Any:
    """MCP 도구를 이름으로 찾아 ainvoke. name 변형으로 재시도."""
    for n in _tool_name_variants(name) + [name]:
        tool = await get_mcp_tool_by_name_async(n)
        if tool is not None:
            try:
                return await tool.ainvoke(kwargs)
            except Exception as e:
                log(f"   ⚠️ MCP 도구 ainvoke 실패 {n}: {e}")
                raise
    raise RuntimeError(f"MCP 도구를 찾을 수 없습니다: {name}")


async def _read_skill_document(skill_name: str, document_path: str) -> str:
    """claude-skills read_skill_document. skill-creator 스크립트 조회.
    응답에 'Document: path' 및 '===...' 래퍼가 있으면 제거해 순수 스크립트만 반환.
    """
    for n in [_TOOL_READ_SKILL_DOCUMENT, f"mcp_claude-skills_{_TOOL_READ_SKILL_DOCUMENT}"]:
        tool = await get_mcp_tool_by_name_async(n)
        if tool is not None:
            out = await tool.ainvoke({
                "skill_name": skill_name,
                "document_path": document_path,
            })
            raw = _extract_text(out)
            return _strip_document_header(raw)
    raise RuntimeError("read_skill_document MCP 도구를 찾을 수 없습니다. (claude-skills)")


def _detect_feedback_language(text: str) -> str:
    """
    매우 간단한 휴리스틱으로 피드백의 주 사용 언어를 추정합니다.
    - 한글(가-힣)이 하나라도 있으면 'ko'
    - 아니면 'en' (기본값)
    """
    if not isinstance(text, str):
        return "en"
    for ch in text:
        # 한글 완성형 범위
        if "\uac00" <= ch <= "\ud7a3":
            return "ko"
    return "en"


# Related skills context: SKILL.md summary chars, file head chars, total cap
_RELATED_SKILL_SUMMARY_CHARS = 700
_RELATED_SKILL_FILE_HEAD_CHARS = 1500
_RELATED_SKILL_CONTEXT_CAP = 20000


def _extract_document_paths_from_find_helpful_result(res: Any) -> Dict[str, List[str]]:
    """
    Extract built-in skill document paths from claude-skills find_helpful_skills output.
    Tries to be resilient across dict/list/text outputs.
    Returns: {skill_name: [doc_path, ...]}
    """
    def _try_json(s: str) -> Any:
        try:
            return json.loads(s)
        except Exception:
            return None

    obj: Any = res
    # unwrap text -> json if possible
    if isinstance(obj, str):
        j = _try_json(obj)
        if j is not None:
            obj = j
    else:
        # sometimes tools return content blocks; attempt to extract text then json
        txt = _extract_text(obj)
        if txt and txt.strip().startswith("{"):
            j = _try_json(txt.strip())
            if j is not None:
                obj = j

    skills: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        v = obj.get("skills") or obj.get("results") or []
        if isinstance(v, list):
            skills = [x for x in v if isinstance(x, dict)]
    elif isinstance(obj, list):
        skills = [x for x in obj if isinstance(x, dict)]

    out: Dict[str, List[str]] = {}
    for s in skills:
        name = (s.get("name") or s.get("skill_name") or s.get("id") or "").strip()
        if not name:
            continue
        docs = s.get("documents") or s.get("document_paths") or s.get("files") or []
        paths: List[str] = []
        if isinstance(docs, list):
            for d in docs:
                if isinstance(d, str):
                    paths.append(d.strip())
                elif isinstance(d, dict):
                    p = (d.get("path") or d.get("document_path") or d.get("name") or "").strip()
                    if p:
                        paths.append(p)
        # ensure deterministic / remove empties
        paths = [p for p in paths if p]
        if paths:
            out[name] = paths
    return out


async def _find_helpful_skills_documents(
    *,
    tenant_id: Optional[str],
    allowed_skill_names: List[str],
    task_description: str,
) -> Dict[str, List[str]]:
    """
    Use claude-skills find_helpful_skills with list_documents=True to obtain document paths
    for built-in skills. Returns {skill_name: [doc_path, ...]}.
    """
    if not allowed_skill_names:
        return {}
    if not tenant_id:
        return {}
    for tool_name in ("find_helpful_skills", "mcp_claude-skills_find_helpful_skills"):
        tool = await get_mcp_tool_by_name_async(tool_name)
        if tool is None:
            continue
        res = await tool.ainvoke(
            {
                "tenant_id": tenant_id,
                "task_description": task_description,
                "top_k": max(3, len(allowed_skill_names)),
                "list_documents": True,
                "allowed_skill_names": allowed_skill_names,
            }
        )
        return _extract_document_paths_from_find_helpful_result(res)
    log("   ⚠️ find_helpful_skills MCP 도구를 찾을 수 없습니다. (claude-skills)")
    return {}


async def _build_related_skills_context(
    *,
    related_skill_ids: Optional[str],
    exclude_skill_id: Optional[str],
    tenant_id: Optional[str],
    task_description: str,
) -> Optional[str]:
    """
    Build a context string from related skills: summary (SKILL.md head) + file paths + each file's head.
    Supports both uploaded skills (HTTP) and built-in skills (claude-skills find_helpful_skills/read_skill_document).
    """
    if not related_skill_ids or not str(related_skill_ids).strip():
        return None
    names = [s.strip() for s in str(related_skill_ids).split(",") if s.strip()]
    if not names:
        return None
    exclude = (exclude_skill_id or "").strip()
    if exclude:
        names = [n for n in names if n != exclude]
    if not names:
        return None
    # 컨텍스트로 넘기는 스킬 개수는 최대 3개
    names = names[:3]

    # Prefer MCP (find_helpful_skills + read_skill_document) for doc lists/content.
    # Fall back to HTTP get_skill_files/get_skill_file_content when MCP doesn't have the skill.
    docs_map = await _find_helpful_skills_documents(
        tenant_id=tenant_id,
        allowed_skill_names=names,
        task_description=task_description,
    )

    parts: List[str] = []
    total = 0
    cap = _RELATED_SKILL_CONTEXT_CAP

    for skill_name in names:
        if total >= cap:
            break

        # ---- summary + file list ----
        try:
            # MCP-first
            try:
                content = (await _read_skill_document(skill_name, "SKILL.md") or "").strip()
            except Exception:
                info = get_skill_file_content(skill_name, "SKILL.md")
                content = (info.get("content") or "").strip()
            summary = content[:_RELATED_SKILL_SUMMARY_CHARS]
            if len(content) > _RELATED_SKILL_SUMMARY_CHARS:
                summary += "..."
            block = f"\n[관련 스킬: {skill_name}]\n요약:\n{summary}\n"
        except Exception as e:
            log(f"   ⚠️ 관련 스킬 SKILL.md 조회 실패 ({skill_name}), 건너뜀: {e}")
            continue

        paths: List[str] = []
        # MCP doc list if available
        paths = [p for p in (docs_map.get(skill_name, []) or []) if p and p != "SKILL.md"]
        if not paths:
            # HTTP fallback
            try:
                files = get_skill_files(skill_name) or []
                for fi in files:
                    p = (fi.get("path") or "").strip()
                    if p and p != "SKILL.md":
                        paths.append(p)
            except Exception as e:
                log(f"   ⚠️ 관련 스킬 파일 목록 조회 실패 ({skill_name}): {e}")
        # keep deterministic and short
        paths = list(dict.fromkeys(paths))
        block += f"파일 목록: {paths}\n"
        block += f"참조 시 경로 형식(필수): {skill_name}/폴더/파일 (예: {skill_name}/references/workflows.md)\n"

        part_len = len(block)
        if total + part_len > cap:
            block = block[: cap - total]
            part_len = len(block)
        parts.append(block)
        total += part_len

        # ---- file heads ----
        for path in paths:
            if total >= cap:
                break
            try:
                # MCP-first, HTTP fallback
                try:
                    c = (await _read_skill_document(skill_name, path) or "").strip()
                except Exception:
                    fc = get_skill_file_content(skill_name, path)
                    c = (fc.get("content") or "").strip()
                head = c[:_RELATED_SKILL_FILE_HEAD_CHARS]
                if len(c) > _RELATED_SKILL_FILE_HEAD_CHARS:
                    head += "..."
                line = f"\n--- [{path}] ---\n{head}\n"
                line_len = len(line)
                if total + line_len > cap:
                    line = line[: cap - total]
                    line_len = len(line)
                parts.append(line)
                total += line_len
            except Exception as e:
                log(f"   ⚠️ 관련 스킬 파일 내용 조회 실패 ({skill_name}/{path}): {e}")

    if not parts:
        return None
    return "".join(parts).strip()


_SKILL_CREATOR_SYSTEM = """You implement the skill-creator workflow. Given user feedback and (for UPDATE) existing skill content, produce a JSON object that will become SKILL.md and bundled files. Follow skill-creator rules. Aim for the same depth and structure as built-in skills (e.g. skill-creator, doc-coauthoring): concrete procedures, principles, reference paths, and examples.

- name: hyphen-case (e.g. my-investment-skill). For UPDATE use the provided existing name.
- description: 1–2 sentences for frontmatter; include WHAT the skill does and WHEN to use it.
- overview: short body overview (used only when body_markdown is omitted).
- steps: list of strings, e.g. ["Step one", "Step two"]. Used only when body_markdown is omitted.
- usage: optional string. Used only when body_markdown is omitted.
- body_markdown: (optional, strongly recommended) Full markdown body for SKILL.md **without frontmatter**. When provided, overview/steps/usage are ignored for the body. Keep under ~500 lines. Include: Overview, When to Use (if needed), Core Principles or Capabilities, step-by-step process (with ### subsections where helpful), explicit references to bundled files, code blocks and tables where useful. If you add additional_files, the body must reference each of them (path + one-line purpose) for progressive disclosure. **Reference path format**: for files in *this* skill use `folder/file` (e.g. `references/guide.md`, `scripts/run.py`); for files in *related* (external) skills always use `skill-name/folder/file` (e.g. `skill-creator/references/workflows.md`). Related-skill references MUST include the skill name prefix.
- additional_files: dict of path -> full content. **CRITICAL**:
  - Include ONLY (a) NEW files with **complete** content, (b) EXISTING files you are **explicitly modifying** with **full** new content.
  - Do NOT include existing files that you are not modifying—they will be preserved automatically.
  - NEVER use placeholders like "content about X", "# code for X", "# code to X". Always provide full, runnable code or full document text.
  - For NEW files: write complete implementation (entire script or entire reference doc). One-line stubs are forbidden.
  - **references/** (e.g. references/*.md): Write as **professional reference docs**: clear ##/### sections, optional table of contents for long docs, "when to read this file" note, code examples, tables, best practices. Avoid one- or two-sentence stub content.
  - **scripts/** (e.g. scripts/*.py): Write **runnable, complete implementations**: module/function docstrings (purpose, args, returns, usage), error handling, type hints where appropriate. No one-line stubs or TODO-only files.
  - **assets/**: Only include files the agent can actually use or modify (templates, boilerplate). Omit if not needed; if included, ensure they are usable as-is.

For UPDATE: preserve existing structure, sections, and files; integrate feedback as additions or refinements. Do not drop existing steps, overview, or files. Merge feedback into the existing content. When modifying a file in additional_files, apply the same quality bar (references = professional reference; scripts = full runnable code).
When [관계 분석] indicates EXTENDS or COMPLEMENTS: **preserve all existing content**; only add or refine from feedback. Do not remove existing steps or files. For additional_files, include only new files or files you are actually changing—omit the rest.

LANGUAGE RULES (CRITICAL):
- Detect the primary language of the user feedback (Korean vs English).
- **All human‑readable natural language text** in SKILL.md and any Markdown/reference files
  MUST be written in the **same language as the feedback**.
- When the feedback is in Korean, write all narrative text, headings, explanations,
  overviews, and steps in clear, natural Korean. Do NOT translate them to English.
- Skill identifiers MUST remain stable and English-friendly:
  - `name` field: keep in hyphenated English (e.g. my-investment-skill).
  - File and directory names under additional_files (e.g. scripts/*.py, references/*.md)
    should be in English and snake_case / hyphen-case as appropriate.
- Code inside scripts (Python, etc.) should use English identifiers and comments unless
  the feedback explicitly requests Korean comments.

If there is any conflict between previous instructions and these language rules,
the language rules take precedence for natural-language content.

When [관련 스킬 참고] is provided: use the listed skills and their file paths to add **explicit references** in overview, usage, or markdown body so that reference graphs can show cross-skill edges. **Reference path rule**: references to files *inside the current skill* use `folder/file` (e.g. `references/guide.md`). References to files in *related (external) skills* MUST use `skill-name/folder/file` (e.g. `skill-creator/references/workflows.md`). Never use bare `folder/file` for external skills—always prefix with the skill name. Do not invent paths; use the skill names and file paths from the context.

Output only valid JSON. No markdown, no explanation. Example: {"name":"x","description":"...","overview":"...","steps":["a","b"],"usage":"","body_markdown":null,"additional_files":{}}"""


async def _generate_skill_artifact_from_feedback(
    feedback_content: str,
    operation: str,
    skill_id: Optional[str],
    existing_skill_md: Optional[str],
    existing_additional_files: Optional[Dict[str, str]],
    relationship_analysis: Optional[str] = None,
    related_skills_context: Optional[str] = None,
) -> Dict:
    """skill-creator 가이드에 따라 피드백과(선택) 기존 스킬 내용으로부터 skill_artifact JSON을 생성."""
    from langchain_core.messages import SystemMessage, HumanMessage

    # 입력 피드백 언어를 감지하여 skill-creator에게 명시적으로 전달
    lang = _detect_feedback_language(feedback_content)
    if lang == "ko":
        lang_hint = (
            "입력 피드백의 주요 언어는 **한국어**입니다.\n"
            "- SKILL.md의 설명, 개요, 단계(steps), 사용법(usage)과 "
            "추가 마크다운/레퍼런스 파일의 본문은 모두 자연스러운 한국어로 작성하세요.\n"
            "- 스킬 이름(name)과 파일/디렉터리 경로, 코드(예: scripts/*.py)는 영어 식별자를 사용하세요.\n"
            "- 사용자가 한국어로 작성했더라도, 스킬 이름과 코드 구조 자체를 영어로 번역/정규화해도 됩니다. "
            "하지만 설명 텍스트를 영어로 바꾸면 안 됩니다."
        )
    else:
        lang_hint = (
            "The primary language of the feedback appears to be **English**.\n"
            "- Write all human‑readable descriptions, overviews, steps, usage text, and "
            "Markdown/reference file bodies in natural English.\n"
            "- Skill `name` and file/directory paths should remain concise, English identifiers.\n"
            "- Code inside scripts should use English identifiers and comments unless explicitly told otherwise."
        )

    user_parts = [lang_hint, f"\n피드백:\n{feedback_content}"]
    if relationship_analysis and str(relationship_analysis).strip():
        user_parts.append(f"\n[관계 분석 (EXTENDS/COMPLEMENTS 시 기존 내용 보존에 참고)]\n{relationship_analysis[:6000]}")
    if related_skills_context and str(related_skills_context).strip():
        user_parts.append(f"\n[관련 스킬 참고 (참조 경로·링크 생성에 활용)]\n{related_skills_context}")
    if operation == "UPDATE" and skill_id:
        user_parts.append(f"\n기존 스킬 이름 (반드시 name에 사용): {skill_id}")
        if existing_skill_md:
            user_parts.append(f"\n기존 SKILL.md:\n{existing_skill_md[:8000]}")
        if existing_additional_files:
            paths = list(existing_additional_files.keys())
            user_parts.append(f"\n기존 additional_files 경로 목록 (수정할 때만 additional_files에 넣고, 전체 내용을 작성. 수정하지 않으면 넣지 말 것—자동 유지됨): {paths}")
            # 수정 시 참고할 수 있도록 파일별 앞부분 전달 (토큰 제한: 파일당 ~2000자, 총 ~20000자)
            total, cap = 0, 20000
            for p, c in list(existing_additional_files.items()):
                if not c or total >= cap:
                    continue
                head = c[:2000] + ("..." if len(c) > 2000 else "")
                user_parts.append(f"\n--- 기존 파일 참고 (수정 시에만 교체) [{p}] ---\n{head}")
                total += len(head)

    llm = create_llm()
    msgs = [SystemMessage(content=_SKILL_CREATOR_SYSTEM), HumanMessage(content="\n".join(user_parts))]
    out = await llm.ainvoke(msgs)
    raw = (getattr(out, "content", None) or "") if out else ""
    raw = (raw or "").strip()
    for prefix in ("```json", "```"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].lstrip()
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"   [skill-creator LLM] JSON 파싱 실패: {e}, raw 앞 400자: {repr(raw[:400])}")
        raise RuntimeError(f"skill-creator LLM 출력 JSON 파싱 실패: {e}") from e
    if not isinstance(obj, dict):
        raise RuntimeError("skill-creator LLM 출력이 JSON 객체가 아님")
    if operation == "UPDATE" and skill_id:
        obj["name"] = skill_id
    if not obj.get("name") and operation == "CREATE":
        obj["name"] = "feedback-skill"
    return obj


def _is_placeholder_overwrite(new_val: str, old_val: Optional[str]) -> bool:
    """
    LLM이 기존 파일을 플레이스홀더('content about X', '# code for X' 등)로 덮어쓸 때
    True를 반환. 이 경우 기존 내용을 유지하고 새 값을 버린다.
    """
    if not old_val or len(old_val) < 400:
        return False
    if not new_val or len(new_val) < 250:
        return True
    low = new_val.lower()
    if any(ph in low for ph in ("content about", "# code for", "# code to ")):
        if len(new_val) < 900:
            return True
    if len(new_val) < len(old_val) * 0.08:
        return True
    return False


def _record_attempted_skill_history(
    *,
    skill_name: str,
    operation: str,
    skill_content_dict: Dict[str, str],
    previous_content: Optional[Dict[str, str]],
    agent_id: str,
    tenant_id: Optional[str],
    feedback_content: Optional[str],
    error: Exception,
) -> None:
    """실패 시에도 의도된 수정 내역(전체 파일 dict)을 JSON으로 변경 이력에 남깁니다."""
    try:
        err_msg = (str(error)[:500]) if error else ""
        new_d = dict(skill_content_dict)
        new_d["SKILL.md"] = (new_d.get("SKILL.md") or "") + (
            f"\n\n<!-- [이력] skill-creator 반영 실패: {err_msg} -->" if err_msg else ""
        )
        record_knowledge_history(
            knowledge_type="SKILL",
            knowledge_id=skill_name,
            agent_id=agent_id,
            tenant_id=tenant_id,
            operation=operation,
            previous_content=previous_content,
            new_content=new_d,
            feedback_content=feedback_content,
            knowledge_name=skill_name,
        )
        log(f"   📝 실패한 시도에 대한 변경 이력 기록 완료: {skill_name}")
    except Exception as e2:
        log(f"   ⚠️ 실패 시 이력 기록 실패 (무시): {e2}")


async def commit_to_skill_via_skill_creator(
    agent_id: str,
    operation: str,
    skill_id: Optional[str] = None,
    feedback_content: Optional[str] = None,
    merge_mode: Optional[str] = None,
    skill_artifact: Optional[Dict] = None,
    relationship_analysis: Optional[str] = None,
    related_skill_ids: Optional[str] = None,
) -> None:
    """
    computer-use + skill-creator로 스킬을 생성/갱신한 뒤 skill_api_client로 업로드.
    skill_artifact가 None이면 피드백과(UPDATE시) 기존 스킬을 바탕으로 skill-creator(LLM)가 생성.
    relationship_analysis가 있으면 스킬 생성 LLM 컨텍스트로 전달(EXTENDS/COMPLEMENTS 시 기존 내용 보존).
    related_skill_ids가 있으면 해당 스킬들의 요약·경로·앞부분을 컨텍스트로 전달(스킬 간 참조 생성용).
    CREATE/UPDATE만 처리 (DELETE는 호출하지 않음).
    """
    # ----- skill_artifact가 없으면 skill-creator(LLM)가 피드백에서 생성 -----
    if skill_artifact is None:
        if not feedback_content or not str(feedback_content).strip():
            raise ValueError("feedback_content가 비어 있습니다. skill-creator가 스킬 내용을 생성하려면 피드백이 필요합니다.")
        if operation == "UPDATE" and (not skill_id or not str(skill_id).strip()):
            raise ValueError("UPDATE 시 skill_id(기존 스킬 이름)가 필요합니다.")
        existing_md: Optional[str] = None
        existing_files: Optional[Dict[str, str]] = None
        if operation == "UPDATE" and skill_id:
            try:
                info = get_skill_file_content(skill_id, "SKILL.md")
                existing_md = info.get("content") or ""
            except Exception as e:
                log(f"   ⚠️ 기존 SKILL.md 조회 실패: {e}")
            try:
                existing_files = {}
                for fi in get_skill_files(skill_id) or []:
                    p = fi.get("path", "")
                    if not p or p == "SKILL.md":
                        continue
                    fc = get_skill_file_content(skill_id, p)
                    c = fc.get("content", "")
                    if c is not None:
                        existing_files[p] = c
            except Exception as e:
                log(f"   ⚠️ 기존 additional_files 조회 실패: {e}")
        related_skills_context: Optional[str] = None
        if related_skill_ids and str(related_skill_ids).strip():
            try:
                agent_info_for_related = _get_agent_by_id(agent_id)
                tenant_id_for_related = agent_info_for_related.get("tenant_id") if agent_info_for_related else None
            except Exception:
                tenant_id_for_related = None
            related_skills_context = await _build_related_skills_context(
                related_skill_ids=related_skill_ids,
                exclude_skill_id=skill_id if operation == "UPDATE" else None,
                tenant_id=tenant_id_for_related,
                task_description=(feedback_content or "")[:200] or "related skills context",
            )
            if related_skills_context:
                log(f"   관련 스킬 컨텍스트 수집 완료 ({len(related_skills_context)}자)")
        log("   skill-creator(LLM)가 피드백으로 스킬 내용 생성 중...")
        skill_artifact = await _generate_skill_artifact_from_feedback(
            feedback_content=feedback_content,
            operation=operation,
            skill_id=skill_id,
            existing_skill_md=existing_md,
            existing_additional_files=existing_files,
            relationship_analysis=relationship_analysis,
            related_skills_context=related_skills_context,
        )
        log(f"   생성된 스킬 name={skill_artifact.get('name')}, steps={len(skill_artifact.get('steps') or [])}")

    skill_name = (skill_id or skill_artifact.get("name") or "").strip()
    if not skill_name:
        raise ValueError("skill_name이 비어 있습니다. skill_id 또는 skill_artifact.name 필요.")

    steps = skill_artifact.get("steps", [])
    description = skill_artifact.get(
        "description",
        f"{skill_name} 작업을 수행하기 위한 단계별 절차입니다.",
    )
    overview = skill_artifact.get("overview")
    usage = skill_artifact.get("usage")
    body_markdown = skill_artifact.get("body_markdown")
    art_files = skill_artifact.get("additional_files") or {}

    agent_info = _get_agent_by_id(agent_id)
    if not agent_info:
        raise ValueError(f"에이전트를 찾을 수 없습니다: {agent_id}")
    tenant_id = agent_info.get("tenant_id")
    if not tenant_id:
        raise ValueError(f"에이전트의 tenant_id가 없습니다: {agent_id}")

    # ----- 1) 기존 스킬 수집 (UPDATE) -----
    existing_files: Dict[str, str] = {}
    previous_content_dict: Optional[Dict[str, str]] = None
    if operation == "UPDATE":
        if not check_skill_exists(skill_name):
            log(f"   ⚠️ UPDATE 대상 스킬이 없어 CREATE로 처리: {skill_name}")
            operation = "CREATE"
        else:
            try:
                info = get_skill_file_content(skill_name, "SKILL.md")
                prev = info.get("content", "")
                if prev:
                    existing_files["SKILL.md"] = prev
            except Exception as e:
                log(f"   ⚠️ 기존 SKILL.md 조회 실패: {e}")
            for fi in get_skill_files(skill_name) or []:
                path = fi.get("path", "")
                if not path or path == "SKILL.md":
                    continue
                try:
                    fc = get_skill_file_content(skill_name, path)
                    c = fc.get("content", "")
                    if c is not None:
                        existing_files[path] = c
                except Exception as e:
                    log(f"   ⚠️ 기존 파일 조회 실패 {path}: {e}")
            # 변경 이력용: 덮어쓰기 전 상태 보관 (JSON으로 저장)
            previous_content_dict = dict(existing_files)
            # artifact로 덮어쓰기 (플레이스홀더로 기존 본문이 날아가는 것 방지)
            for k, v in art_files.items():
                if _is_placeholder_overwrite(v, existing_files.get(k)):
                    log(f"   [additional_files] 플레이스홀더 감지, 기존 유지: {k}")
                    continue
                existing_files[k] = v
            art_files = existing_files

    # CREATE: artifact만 (body_markdown 있으면 본문으로 사용, 없으면 overview+steps+usage)
    skill_content = _format_skill_document(
        skill_name,
        steps,
        description=description,
        overview=overview,
        usage=usage,
        body_markdown=body_markdown,
    )

    # ----- 2) skill-creator 스크립트 조회 -----
    log("   skill-creator 스크립트 조회 (read_skill_document)...")
    init_script = await _read_skill_document("skill-creator", "scripts/init_skill.py")
    package_script = await _read_skill_document("skill-creator", "scripts/package_skill.py")
    quick_validate_script = get_quick_validate_script()

    # ----- 3) computer-use: create_session -----
    log("   computer-use create_session...")
    cr = await _invoke_tool(_TOOL_CREATE_SESSION, ttl=600)
    session_id = _extract_session_id(cr)
    if not session_id:
        _log_create_session_debug(cr)
        raise RuntimeError("create_session에서 session_id를 얻지 못했습니다.")

    try:
        work = "/tmp/skill_work"
        base = f"/tmp/{skill_name}"
        skill_path = base

        # ----- 4) /tmp/skill_work에 스크립트 생성 -----
        await _invoke_tool(_TOOL_RUN_SHELL, session_id=session_id, command=f"mkdir -p {work}")
        await _invoke_tool(_TOOL_CREATE_FILE, session_id=session_id, file_path=f"{work}/quick_validate.py", content=quick_validate_script)
        await _invoke_tool(_TOOL_CREATE_FILE, session_id=session_id, file_path=f"{work}/package_skill.py", content=package_script)
        await _invoke_tool(_TOOL_CREATE_FILE, session_id=session_id, file_path=f"{work}/init_skill.py", content=init_script)

        if operation == "CREATE":
            # init_skill
            await _invoke_tool(_TOOL_RUN_SHELL, session_id=session_id, command=f"python3 {work}/init_skill.py {skill_name} --path /tmp")
            # SKILL.md 덮어쓰기
            await _invoke_tool(_TOOL_CREATE_FILE, session_id=session_id, file_path=f"{base}/SKILL.md", content=skill_content)
            # additional_files
            for path, content in art_files.items():
                await _invoke_tool(_TOOL_RUN_SHELL, session_id=session_id, command=f"mkdir -p {base}/{str(Path(path).parent)}")
                await _invoke_tool(_TOOL_CREATE_FILE, session_id=session_id, file_path=f"{base}/{path}", content=content)
            # init 기본 예제 제거 (additional_files에 없으면)
            for ex in [f"{base}/scripts/example.py", f"{base}/references/api_reference.md", f"{base}/assets/example_asset.txt"]:
                try:
                    await _invoke_tool(_TOOL_DELETE_FILE, session_id=session_id, file_path=ex)
                except Exception:
                    pass
        else:
            # UPDATE: 디렉터리 및 파일 직접 생성
            await _invoke_tool(_TOOL_RUN_SHELL, session_id=session_id, command=f"mkdir -p {base}/scripts {base}/references {base}/assets")
            await _invoke_tool(_TOOL_CREATE_FILE, session_id=session_id, file_path=f"{base}/SKILL.md", content=skill_content)
            for path, content in art_files.items():
                await _invoke_tool(_TOOL_RUN_SHELL, session_id=session_id, command=f"mkdir -p {base}/{str(Path(path).parent)}")
                await _invoke_tool(_TOOL_CREATE_FILE, session_id=session_id, file_path=f"{base}/{path}", content=content)

        # ----- 5) quick_validate -----
        rv = await _invoke_tool(_TOOL_RUN_SHELL, session_id=session_id, command=f"cd {work} && python3 quick_validate.py {base}")
        out = _extract_text(rv)
        if "Skill is valid" not in out and "valid" not in out.lower():
            raise RuntimeError(f"quick_validate 실패: {out[:500]}")

        # ----- 6) package_skill -----
        pkg_run = await _invoke_tool(_TOOL_RUN_SHELL, session_id=session_id, command=f"cd {work} && python3 package_skill.py {base} /tmp")
        pkg_out = _extract_text(pkg_run)
        pkg = f"/tmp/{skill_name}.skill"

        # .skill 파일 존재 확인 (package_skill 실패 시 생성되지 않음. 조기 실패로 원인 파악 용이)
        check_run = await _invoke_tool(
            _TOOL_RUN_SHELL,
            session_id=session_id,
            command=f'python3 -c \'import os; print("READY" if os.path.isfile("{pkg}") else "MISSING")\'',
        )
        check_txt = (_extract_text(check_run) or "").strip()
        if "READY" not in check_txt:
            log(f"   ⚠️ .skill 존재 확인: check_txt={repr(check_txt)}, package_skill 출력 일부: {repr(pkg_out[:500])}")
            raise RuntimeError(
                f".skill 파일이 생성되지 않았습니다: {pkg}. "
                f"package_skill이 실패했거나 출력 경로가 다를 수 있습니다. package_skill 출력: {pkg_out[:800]}"
            )

        # ----- 7) .skill 회수 (base64, 텍스트 stdout로 출력해 MCP 캡처 안정화) -----
        rb = await _invoke_tool(
            _TOOL_RUN_SHELL,
            session_id=session_id,
            command=f'python3 -c \'import base64; d=open("{pkg}","rb").read(); print(base64.b64encode(d).decode("ascii"), end="")\'',
        )
        # [디코딩 디버그] run_shell 응답 구조
        log(f"   [base64] run_shell 응답: type={type(rb).__name__}")
        if isinstance(rb, dict):
            for k in list(rb.keys())[:12]:
                v = rb[k]
                if isinstance(v, str):
                    log(f"   [base64]   rb[{k!r}] str len={len(v)}, 앞200={repr(v[:200])}, 뒤100={repr(v[-100:])}")
                elif isinstance(v, list):
                    texts = [x.get("text", x) if isinstance(x, dict) else str(x) for x in v[:5]]
                    lens = [len(t) if isinstance(t, str) else 0 for t in texts]
                    log(f"   [base64]   rb[{k!r}] list len={len(v)}, 요소0~4 text길이={lens}, 합={sum(lens)}")
                    if texts and isinstance(texts[0], str) and texts[0]:
                        log(f"   [base64]     [0] 앞120={repr(texts[0][:120])}, 뒤80={repr(texts[0][-80:])}")
                else:
                    log(f"   [base64]   rb[{k!r}]={type(v).__name__}")
        raw = _extract_text(rb)
        log(f"   [base64] _extract_text(rb) → len={len(raw)}, 앞250={repr(raw[:250])}, 뒤250={repr(raw[-250:])}")

        # run_shell 실패 시 stderr(트레이스백)가 반환될 수 있음. base64 디코딩 전 검사
        if raw and (
            "FileNotFoundError" in raw
            or "No such file or directory" in raw
            or ("STDERR:" in raw and "Traceback" in raw)
        ):
            raise RuntimeError(
                f".skill 파일을 찾을 수 없거나 base64 읽기 명령이 실패했습니다: {pkg}. "
                f"run_shell이 stderr(트레이스백)를 반환한 것으로 보입니다. package_skill 출력 경로·실행 결과를 확인하세요. 원문: {raw[:500]}"
            )
        try:
            zip_bytes = _normalize_and_decode_base64(raw)
        except Exception as e:
            log(f"   ⚠️ .skill base64 디코딩 실패: {e}")
            raise RuntimeError(f".skill base64 디코딩 실패: {e}") from e

    except Exception as e:
        _record_attempted_skill_history(
            skill_name=skill_name,
            operation=operation,
            skill_content_dict={"SKILL.md": skill_content, **art_files},
            previous_content=previous_content_dict,
            agent_id=agent_id,
            tenant_id=tenant_id,
            feedback_content=feedback_content,
            error=e,
        )
        raise

    finally:
        try:
            await _invoke_tool(_TOOL_DELETE_SESSION, session_id=session_id)
        except Exception as e:
            log(f"   ⚠️ delete_session 실패 (무시): {e}")

    # ----- 8) zip 파싱: skill_content + additional_files -----
    # ----- 9) skill_api_client로 업로드/수정 및 이력 -----
    try:
        prefix = f"{skill_name}/"
        out_content = ""
        out_files: Dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                if n == f"{prefix}SKILL.md" or n == f"{skill_name}\\SKILL.md":
                    out_content = zf.read(n).decode("utf-8", errors="replace")
                    continue
                if n.startswith(prefix):
                    rel = n[len(prefix) :]
                elif n.startswith(skill_name + "\\"):
                    rel = n[len(skill_name) + 1 :].replace("\\", "/")
                else:
                    continue
                if rel == "SKILL.md":
                    out_content = zf.read(n).decode("utf-8", errors="replace")
                    continue
                try:
                    out_files[rel] = zf.read(n).decode("utf-8", errors="replace")
                except Exception:
                    pass
        if not out_content:
            raise RuntimeError(".skill 내 SKILL.md를 찾을 수 없습니다.")

        # 변경 이력: 모든 파일을 JSON 구조로 저장 (TEXT 컬럼에 json.dumps)
        new_content_dict: Dict[str, str] = {"SKILL.md": out_content, **out_files}

        if operation == "CREATE":
            upload_skill(skill_name=skill_name, skill_content=out_content, tenant_id=tenant_id, additional_files=out_files or None)
            update_agent_and_tenant_skills(agent_id, skill_name, "CREATE")
            record_knowledge_history(
                knowledge_type="SKILL",
                knowledge_id=skill_name,
                agent_id=agent_id,
                tenant_id=tenant_id,
                operation="CREATE",
                new_content=new_content_dict,
                feedback_content=feedback_content,
                knowledge_name=skill_name,
            )
            log(f"   ✅ SKILL(skill-creator) CREATE 완료: {skill_name}")
        else:
            update_skill_file(skill_name, "SKILL.md", content=out_content)
            for p, c in out_files.items():
                try:
                    update_skill_file(skill_name, p, content=c)
                except Exception as e:
                    log(f"   ⚠️ 파일 업데이트 실패 {p}: {e}")
            update_agent_and_tenant_skills(agent_id, skill_name, "UPDATE")
            record_knowledge_history(
                knowledge_type="SKILL",
                knowledge_id=skill_name,
                agent_id=agent_id,
                tenant_id=tenant_id,
                operation="UPDATE",
                previous_content=previous_content_dict,
                new_content=new_content_dict,
                feedback_content=feedback_content,
                knowledge_name=skill_name,
            )
            log(f"   ✅ SKILL(skill-creator) UPDATE 완료: {skill_name}")
    except Exception as e:
        _record_attempted_skill_history(
            skill_name=skill_name,
            operation=operation,
            skill_content_dict={"SKILL.md": out_content, **out_files},
            previous_content=previous_content_dict,
            agent_id=agent_id,
            tenant_id=tenant_id,
            feedback_content=feedback_content,
            error=e,
        )
        raise
