"""
Deep Agent 기반 피드백 처리
deepagents 라이브러리를 사용하여 피드백 → 스킬 개선 수행
"""

import os
from typing import Dict, List, Optional, Any

from deepagents import create_deep_agent
from core.llm import create_llm
from core.skill_tools import create_skill_tools
from utils.logger import log, handle_error


SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


SYSTEM_PROMPT = """당신은 에이전트 피드백을 분석하여 스킬(SKILL)을 개선하는 전문가입니다.

**역할:**
피드백을 분석하고, 기존 스킬을 검색하여, 적절한 스킬 생성/수정/삭제/적재를 수행합니다.

**⚠️ 핵심 원칙: 행동하기 전에 깊이 생각하세요**
성급한 행동은 기존 스킬을 손상시킵니다. 모든 결정에는 명확한 근거가 필요합니다.

**단순 재시도 요청 처리:**
피드백이 "다시 시도", "재시도", "try again" 등의 단순 재시도 요청이면, 스킬 변경 없이 처리를 종료하세요.

**스킬 저장소:**
- SKILL: 단계별 절차, 작업 순서 (예: "먼저 X를 하고, 그 다음 Y를 한다")
- 스킬은 SKILL.md 파일과 부가 파일들로 구성됩니다.
- 스킬 내용(SKILL.md, steps, additional_files)은 skill-creator가 생성합니다.

**사용 가능한 도구:**
1. `search_similar_skills` - 피드백과 유사한 기존 스킬 검색
2. `get_skill_detail` - 기존 스킬의 상세 내용 조회
3. `commit_to_skill` - 스킬 생성/수정/삭제 (skill-creator가 내용 생성)
4. `attach_skills_to_agent` - 기존 스킬을 에이전트에 적재

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 필수 추론 프레임워크
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### [STEP 1] 피드백 의도 분석
- 단순 재시도 요청인가? → 즉시 종료
- 핵심 지식은 무엇인가? (절차, 작업 순서)
- 새로운 절차인가? 기존 절차의 수정인가?
- 적용 조건/범위가 명시되어 있는가?

### [STEP 2] 기존 스킬 심층 파악
`search_similar_skills`로 기존 스킬을 조회한 후:
- 기존 스킬의 적용 범위와 조건은?
- 피드백의 범위와 기존 스킬의 범위가 겹치는가?
- 기존 스킬에서 반드시 보존해야 할 부분은?

**스킬 판단 기준:**
- 목표/결과는 스킬 절차가 아님 ("사업부 의사결정에 기여" 등은 스킬이 아님)
- 스킬은 구체적 작업 절차·산출물만 담음
- 기존 스킬이 절차를 이미 커버하면 `attach_skills_to_agent`만 사용

### [STEP 3] 관계 분석 및 작업 결정

**관계 유형:**
| 유형 | 정의 | 처리 |
|------|------|------|
| DUPLICATE | 동일 내용 | 기존 스킬이 에이전트에 없으면 attach |
| EXTENDS | 새 조건/케이스 추가 | 기존 + 새 내용 추가 (UPDATE) |
| REFINES | 세부사항 변경 | 해당 부분만 수정 (UPDATE) |
| CONFLICTS | 상충/모순 | 판단 필요 |
| SUPERSEDES | 명시적 대체 | 삭제 후 새로 생성 |
| COMPLEMENTS | 다른 측면 | 기존 커버하면 attach, 새 절차 필요하면 CREATE |
| UNRELATED | 무관 | 새로 생성 (CREATE) |

### [STEP 4] 자기 검증
작업 전 반드시 확인:
- 내 판단이 틀렸다면, 다른 가능한 해석은?
- 이 작업 후 기존 스킬이 손상되는 부분이 있는가?
- 최종 결과가 피드백의 의도와 기존 스킬 모두를 반영하는가?

### [STEP 5] 작업 실행
- **attach**: 기존 스킬로 충분할 때 → `attach_skills_to_agent`
- **CREATE**: 새 절차 필요 → `commit_to_skill(operation="CREATE")`
- **UPDATE**: 기존 수정 → `commit_to_skill(operation="UPDATE", skill_id="...")`
- **DELETE**: 삭제 → `commit_to_skill(operation="DELETE", skill_id="...")`
- **IGNORE**: 변경 불필요 시 아무 도구도 호출하지 않음

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ⚠️ 주의사항
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- 스킬 내용(SKILL.md)은 직접 작성하지 마세요. skill-creator가 생성합니다.
- commit_to_skill 호출 시 피드백 내용이 자동으로 전달됩니다.
- UPDATE 시 skill_id는 반드시 기존 스킬 이름을 사용하세요.
- 스킬 변경이 필요하면 반드시 도구를 호출하세요. 말로만 결론을 내면 안 됩니다.
"""


def _format_feedback_input(
    agent_info: Dict,
    feedback_content: str,
    task_description: str = "",
    events: Optional[List[Dict[str, Any]]] = None,
) -> str:
    agent_info_text = (
        f"ID: {agent_info.get('id', '')}, "
        f"이름: {agent_info.get('name', '')}, "
        f"역할: {agent_info.get('role', '')}, "
        f"목표: {agent_info.get('goal', '')}"
    )

    events_summary = "없음"
    if events:
        import json
        lines = []
        for ev in events[:50]:
            ev_type = ev.get("event_type", "")
            status = ev.get("status", "")
            crew_type = ev.get("crew_type", "")
            ts = ev.get("timestamp", "")
            data_str = ""
            try:
                data = ev.get("data", {})
                data_str = json.dumps(data, ensure_ascii=False)
                if len(data_str) > 300:
                    data_str = data_str[:300] + "...(truncated)"
            except Exception:
                data_str = str(ev.get("data", ""))[:300]
            lines.append(f"- time={ts}, type={ev_type}, status={status}, crew_type={crew_type}, data={data_str}")
        events_summary = "\n".join(lines)

    return f"""다음 피드백을 분석하여 에이전트의 스킬을 개선해주세요:

**피드백 내용:**
{feedback_content}

**에이전트 정보:**
{agent_info_text}

**작업 지시사항:**
{task_description}

**이벤트 로그:**
{events_summary}

위 정보를 바탕으로 기존 스킬을 검색하고, 필요한 스킬 생성/수정/삭제/적재를 수행하세요."""


async def process_feedback_with_deep_agent(
    agent_id: str,
    agent_info: Dict,
    feedback_content: str,
    task_description: str = "",
    events: Optional[List[Dict[str, Any]]] = None,
) -> Dict:
    """
    Deep Agent를 사용하여 피드백을 처리하고 스킬을 개선합니다.

    Args:
        agent_id: 에이전트 ID
        agent_info: 에이전트 정보
        feedback_content: 피드백 내용
        task_description: 작업 지시사항
        events: 이벤트 로그

    Returns:
        처리 결과 dict
    """
    try:
        log(f"🤖 Deep Agent 기반 피드백 처리 시작: agent_id={agent_id}")

        # 커스텀 스킬 도구 생성 (agent_id, feedback_content 바인딩)
        skill_tools = create_skill_tools(agent_id, feedback_content=feedback_content)

        # LLM 생성
        llm = create_llm(streaming=False, temperature=0)

        # skills 디렉토리 설정
        skills_paths = []
        if os.path.isdir(SKILLS_DIR):
            skills_paths.append(SKILLS_DIR)
            log(f"   📁 스킬 디렉토리 로드: {SKILLS_DIR}")
        else:
            log(f"   ⚠️ 스킬 디렉토리 없음: {SKILLS_DIR} (skills 파라미터 미사용)")

        # Deep Agent 생성
        agent = create_deep_agent(
            model=llm,
            tools=skill_tools,
            system_prompt=SYSTEM_PROMPT,
            skills=skills_paths if skills_paths else None,
            debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes", "on"),
        )

        # 입력 텍스트 생성
        input_text = _format_feedback_input(
            agent_info=agent_info,
            feedback_content=feedback_content,
            task_description=task_description,
            events=events,
        )

        log(f"🔄 Deep Agent 실행 시작...")
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": input_text}]},
        )

        # 결과 처리
        messages = result.get("messages", [])
        output = ""
        used_tools = []

        for msg in messages:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    used_tools.append(tc.get("name", ""))
            if hasattr(msg, "type") and msg.type == "ai" and hasattr(msg, "content"):
                content = msg.content
                if isinstance(content, list):
                    content = "\n".join(
                        b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if content:
                    output = content

        # commit 도구 호출 여부 확인
        committed_tools = {"commit_to_skill", "attach_skills_to_agent"}
        did_commit = any(t in committed_tools for t in used_tools)

        # 말로만 결론을 내고 commit하지 않은 경우 체크
        output_lower = (output or "").lower()
        claims_mutation = any(
            kw in output_lower
            for kw in ["create", "update", "delete", "저장", "생성", "수정", "삭제", "커밋"]
        )
        claims_ignore = any(
            kw in output_lower
            for kw in ["ignore", "무시", "저장하지", "처리하지", "변경 불필요", "재시도"]
        )

        if not did_commit and claims_mutation and not claims_ignore:
            err = "Deep Agent가 저장/수정/삭제 결론을 냈지만 도구를 호출하지 않아 실제 변경이 저장되지 않았습니다. (no_commit)"
            log(f"❌ {err}")
            return {
                "output": output,
                "agent_id": agent_id,
                "error": err,
                "used_tools": used_tools,
            }

        log(f"✅ Deep Agent 처리 완료")
        log(f"   최종 출력: {output[:200]}...")
        log(f"   사용 도구: {used_tools}")

        return {
            "output": output,
            "agent_id": agent_id,
            "used_tools": used_tools,
            "did_commit": did_commit,
        }

    except Exception as e:
        log(f"❌ Deep Agent 피드백 처리 중 에러: {str(e)[:300]}...")
        handle_error("DeepAgent피드백처리", e)
        return {
            "output": "피드백 처리 중 에러 발생",
            "agent_id": agent_id,
            "error": str(e),
        }
