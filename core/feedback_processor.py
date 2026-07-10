import json
from typing import Dict, List, Any, Optional
from utils.logger import log, handle_error
from core.llm import create_llm


def clean_json_response(content: str) -> str:
    content = content.replace("```json", "").replace("```", "")
    return content.strip()


async def match_feedback_to_agents(
    feedback: str,
    agents: List[Dict],
    task_description: str = "",
    events: Optional[List[Dict[str, Any]]] = None,
) -> Dict:
    """
    AI를 사용해 피드백을 각 에이전트에 매칭하고 스킬 개선용 학습 후보를 생성합니다.
    """

    llm = create_llm(streaming=False, temperature=0)

    agents_info = "\n".join([
        f"- 에이전트 ID: {agent['id']}, 이름: {agent['name']}, 역할: {agent['role']}, 목표: {agent['goal']}"
        for agent in agents
    ])

    events_summary = "없음"
    if events:
        lines = []
        for ev in events[:30]:
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
            lines.append(
                f"- time={ts}, type={ev_type}, status={status}, crew_type={crew_type}, data={data_str}"
            )
        events_summary = "\n".join(lines)

    prompt = f"""
다음 상황을 분석하여 각 에이전트에게 적절한 스킬 개선 피드백을 생성해주세요.

**작업 지시사항:**
{task_description}

**사용자 피드백 (시간순):**
{feedback}

**해당 작업의 이벤트 로그 (시간순, 실제 스킬/도구 사용 내역):**
{events_summary}

**에이전트 목록:**
{agents_info}

**상황 설명:**
에이전트들이 위의 작업지시사항에 따라 작업을 수행했지만, 사용자가 피드백을 제공했습니다.
이 피드백을 바탕으로 에이전트의 스킬(작업 절차)을 개선하기 위한 학습 후보를 생성하세요.

**피드백 처리 방식:**
- 가장 최신(time이 늦은) 피드백을 최우선으로 반영
- 이전 피드백들은 맥락 참고용
- 최신 피드백의 요구사항이 이전과 다르면 최신 것을 따름
- 자연스럽고 통합된 하나의 완전한 피드백으로 작성
- 최대 2500자까지 허용하여 상세히 작성

**스킬 개선 초점:**
- 피드백에서 절차(작업 순서, 단계별 프로세스)와 관련된 내용을 추출
- 기존 스킬의 수정이 필요한지, 새 스킬 생성이 필요한지 판단 힌트 제공
- 에이전트가 스킬을 개선할 수 있는 구체적이고 실행 가능한 가이드 제공

**응답 형식:**
- 추가 설명 없이 오직 아래 JSON 구조로만 응답하세요
- JSON 객체만 출력하세요

{{
  "agent_feedbacks": [
    {{
      "agent_id": "에이전트_ID",
      "agent_name": "에이전트_이름",
      "learning_candidate": {{
        "content": "시간순 피드백들을 통합한 자연스러운 스킬 개선 가이드",
        "intent_hint": "이 피드백이 어떤 스킬 개선을 요구하는지에 대한 요약 힌트"
      }}
    }}
  ]
}}
"""

    try:
        response = await llm.ainvoke(prompt)
        cleaned_content = clean_json_response(response.content)

        log(f"📤 LLM 전체 응답: {cleaned_content}")

        parsed_result = json.loads(cleaned_content)
        if parsed_result.get("agent_feedbacks"):
            for fb in parsed_result["agent_feedbacks"]:
                lc = fb.get('learning_candidate', {})
                log(f"📝 에이전트 '{fb.get('agent_name', 'Unknown')}' 학습 후보:")
                log(f"   내용: {lc.get('content', 'No content')}")
                log(f"   의도 힌트: {lc.get('intent_hint', 'No hint')}")

        return parsed_result
    except json.JSONDecodeError as e:
        log(f"❌ JSON 파싱 실패 - 응답: {response.content if 'response' in locals() else 'None'}")
        handle_error("피드백매칭 JSON 파싱", f"응답 파싱 실패: {e}")
        return {"agent_feedbacks": []}
    except Exception as e:
        handle_error("피드백매칭", e)
        return {"agent_feedbacks": []}


async def extract_general_rule(
    collected_items: List[Dict],
    task_description: str = "",
) -> Optional[str]:
    """배치에 모인 피드백들에서 공통 일반 규칙만 추출한다.

    이 단계는 스킬 개선 작업이 아니다 — 어떤 스킬을 고칠지 결정하거나 SKILL.md를
    작성하지 않는다. 피드백들 사이에 명확한 공통 규칙이 없으면 None을 반환한다
    (억지로 저품질 규칙을 만들어내지 않는다).
    """
    llm = create_llm(streaming=False, temperature=0)

    items_sorted = sorted(collected_items, key=lambda x: x.get("time", ""))
    items_summary = "\n".join(
        f"- time={item.get('time', '')}, content={item.get('content', '')}"
        for item in items_sorted
    ) or "없음"

    prompt = f"""
같은 업무 활동 단계에서 수집된 여러 워크아이템의 사용자 피드백입니다.
이 피드백들 사이에 공통적으로 추출 가능한 일반 규칙(가이드라인)이 있는지 판단하세요.

**작업 지시사항 (참고용):**
{task_description}

**수집된 피드백 (시간순):**
{items_summary}

**주의:**
- 아직 어떤 스킬을 고칠지 결정하는 단계가 아닙니다. 스킬 이름을 언급하거나 SKILL.md 내용을 작성하지 마세요.
- 피드백들 사이에 명확한 공통 규칙이 없다면 억지로 만들어내지 마세요 — 이 경우 has_common_rule을 false로 응답하세요.
- 최신(time이 늦은) 피드백이 이전 피드백과 상충하면 최신 것을 우선하되, 이전 것도 맥락으로 반영하세요.

**응답 형식:**
- 추가 설명 없이 오직 아래 JSON 구조로만 응답하세요
- JSON 객체만 출력하세요

{{
  "has_common_rule": true 또는 false,
  "general_rule": "공통 규칙을 자연스럽게 서술한 텍스트 (has_common_rule이 true일 때만 채움)"
}}
"""

    try:
        response = await llm.ainvoke(prompt)
        cleaned_content = clean_json_response(response.content)
        parsed_result = json.loads(cleaned_content)
        rule = parsed_result.get("general_rule")
        if parsed_result.get("has_common_rule") and rule:
            return rule
        return None
    except json.JSONDecodeError as e:
        handle_error("일반규칙추출 JSON 파싱", f"응답 파싱 실패: {e}")
        return None
    except Exception as e:
        handle_error("일반규칙추출", e)
        return None
