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


_VALID_TARGET_TYPES = {"SKILL", "DMN_RULE", "PROCESS_DEFINITION"}


async def classify_and_extract_proposal(
    collected_items: List[Dict],
    task_description: str = "",
) -> List[Dict[str, Any]]:
    """트리거된 배치의 피드백이 무엇을 개선할 수 있는지 먼저 분류하고, 분류된 target마다 제안
    아티팩트를 만든다. 분류와 target별 생성은 한 번의 LLM 호출로 처리한다.

    target 종류:
    - SKILL: 절차/실행 규칙. artifact는 자연어 일반 규칙 텍스트 (기존 extract_general_rule과 동일한
      산출물 — 어떤 스킬을 고칠지 결정하거나 SKILL.md를 작성하지 않는다).
    - DMN_RULE: 조건-결과 형태의 비즈니스 판단 규칙. artifact는 decision/rules를 담은 dict.
    - PROCESS_DEFINITION: 업무 흐름(활동/분기/순서) 자체에 대한 변경. artifact는 activities/
      sequences/gateways 변경안을 담은 dict.

    DMN_RULE/PROCESS_DEFINITION artifact는 proc_def.definition과 같은 JSON 형태를 따르되,
    이 함수는 실제 proc_def를 조회하거나 쓰지 않는다 — 제안 생성까지만 담당한다.

    한 배치가 서로 다른 관심사를 동시에 담고 있으면 여러 target을 함께 반환할 수 있다(MIXED).
    피드백들 사이에 공통 관심사가 전혀 없으면 빈 리스트를 반환한다(억지로 만들어내지 않는다).
    """
    llm = create_llm(streaming=False, temperature=0)

    items_sorted = sorted(collected_items, key=lambda x: x.get("time", ""))
    items_summary = "\n".join(
        f"- time={item.get('time', '')}, content={item.get('content', '')}"
        for item in items_sorted
    ) or "없음"

    prompt = f"""
같은 업무 활동 단계에서 수집된 여러 워크아이템의 사용자 피드백입니다.
이 피드백들이 무엇을 개선하기 위한 것인지 먼저 분류한 뒤, 분류된 대상마다 그에 맞는 제안 내용을 만드세요.

**작업 지시사항 (참고용):**
{task_description}

**수집된 피드백 (시간순):**
{items_summary}

**분류 기준 (하나의 배치가 여러 target에 동시에 해당할 수 있습니다):**

1. **SKILL (절차/실행 규칙)**
   - 작업을 "어떻게" 수행할지에 대한 반복 가능한 절차, 순서, 산출물 형식 관련 피드백
   - 예: "먼저 X를 확인하고 나서 Y를 처리해야 한다", "결과는 항상 표 형식으로 정리해야 한다"

2. **DMN_RULE (의사결정/비즈니스 규칙)**
   - 조건-결과 형태의 판단 기준에 대한 피드백
   - 예: "금액이 100만원 이상이면 추가 승인을 받아야 한다", "VIP 고객이면 우선 처리해야 한다"

3. **PROCESS_DEFINITION (프로세스 흐름)**
   - 개별 작업의 수행 방식이 아니라, 업무 흐름/구조 자체에 대한 피드백 (활동 추가/삭제, 승인 단계 추가,
     담당자/역할 변경, 분기 추가 등)
   - 예: "이 단계 앞에 팀장 승인 단계가 빠져있다", "이 활동은 다른 역할이 담당해야 한다"

**주의:**
- 아직 실제로 스킬/DMN/프로세스 정의를 조회하거나 수정하는 단계가 아닙니다. 기존 스킬 이름이나 기존
  프로세스 정의의 실제 내용을 안다고 가정하지 마세요 (조회 없이 피드백만으로 판단하세요).
- 피드백들 사이에 명확한 공통 관심사가 없다면 억지로 만들어내지 마세요 — 이 경우 targets를 빈 배열로 응답하세요.
- 최신(time이 늦은) 피드백이 이전 피드백과 상충하면 최신 것을 우선하되, 이전 것도 맥락으로 반영하세요.
- 서로 다른 관심사(예: 절차 문제 하나 + 비즈니스 규칙 문제 하나)가 섞여 있으면 각각 별도 target으로
  분리해 응답하세요. 같은 관심사를 여러 target에 중복으로 넣지 마세요.

**응답 형식:**
- 추가 설명 없이 오직 아래 JSON 구조로만 응답하세요
- JSON 객체만 출력하세요

{{
  "targets": [
    {{
      "type": "SKILL",
      "artifact": "공통 절차 규칙을 자연스럽게 서술한 텍스트"
    }},
    {{
      "type": "DMN_RULE",
      "artifact": {{
        "decision": {{"name": "의사결정 이름", "description": "이 의사결정이 판단하는 것"}},
        "rules": [
          {{"when": "조건 (자연어)", "then": "결과/행동 (자연어)", "condition": "조건 (표현식 형태, 알 수 있는 경우)", "target": "결과가 가리키는 대상 (선택)"}}
        ]
      }}
    }},
    {{
      "type": "PROCESS_DEFINITION",
      "artifact": {{
        "summary": "흐름 변경 요약",
        "activities": [
          {{"change_type": "ADD 또는 MODIFY", "id": "활동 id (신규면 임의 지정)", "name": "활동명", "role": "담당 역할", "note": "변경 내용 설명"}}
        ],
        "sequences": [
          {{"change_type": "ADD 또는 MODIFY", "from": "출발 활동/게이트웨이", "to": "도착 활동/게이트웨이", "condition": "분기 조건 (선택)", "note": "변경 내용 설명"}}
        ],
        "gateways": [
          {{"change_type": "ADD 또는 MODIFY", "id": "게이트웨이 id (신규면 임의 지정)", "type": "exclusiveGateway 등", "note": "변경 내용 설명"}}
        ]
      }}
    }}
  ]
}}

- targets 배열에는 실제로 해당하는 타입만 포함하세요 (해당 없는 타입은 배열에서 완전히 제외).
- 공통 관심사가 전혀 없으면 "targets": [] 로 응답하세요.
"""

    try:
        response = await llm.ainvoke(prompt)
        cleaned_content = clean_json_response(response.content)
        parsed_result = json.loads(cleaned_content)
        raw_targets = parsed_result.get("targets") or []

        targets: List[Dict[str, Any]] = []
        for t in raw_targets:
            if not isinstance(t, dict):
                continue
            ttype = t.get("type")
            artifact = t.get("artifact")
            if ttype not in _VALID_TARGET_TYPES or not artifact:
                continue
            targets.append({"type": ttype, "artifact": artifact})
        return targets
    except json.JSONDecodeError as e:
        handle_error("피드백분류 JSON 파싱", f"응답 파싱 실패: {e}")
        return []
    except Exception as e:
        handle_error("피드백분류", e)
        return []


async def resolve_skill_identity(artifact_text: str, candidates: List[Dict[str, Any]]) -> Dict[str, str]:
    """SKILL target이 CREATE(새 스킬)인지 UPDATE(기존 스킬)인지, 어떤 이름을 쓸지 결정한다.

    candidates는 유사도 점수로 미리 걸러내지 않고 이름+설명 전체를 그대로 LLM에 보여준다
    (progressive disclosure) — 임베딩 유사도 검색 대신 LLM이 직접 읽고 판단하게 한다.
    실패 시 CREATE + artifact 요약 기반 이름으로 폴백한다(항상 결과를 반환).
    """
    llm = create_llm(streaming=False, temperature=0)

    candidates_text = "\n".join(
        f"- 이름: {c.get('name', '')}, 설명: {c.get('description', '')}" for c in candidates
    ) or "없음"

    prompt = f"""
아래 피드백(스킬 절차 규칙)이 기존 스킬 중 하나를 고치는 것인지, 새 스킬이 필요한지 판단하세요.

**피드백(제안된 규칙):**
{artifact_text}

**기존 스킬 목록:**
{candidates_text}

**판단 기준:**
- 기존 스킬 중 하나와 다루는 절차/범위가 명확히 겹치면 UPDATE, 그 스킬의 정확한 이름을 그대로 쓰세요.
- 겹치는 기존 스킬이 없으면 CREATE, 피드백 내용을 짧고 명확하게 나타내는 새 이름을 지으세요
  (소문자, 하이픈으로 구분된 kebab-case, 예: minwon-urgency-evidence-protocol).

**응답 형식(JSON만):**
{{"decision": "CREATE 또는 UPDATE", "name": "스킬 이름"}}
"""

    try:
        response = await llm.ainvoke(prompt)
        parsed = json.loads(clean_json_response(response.content))
        decision = (parsed.get("decision") or "CREATE").strip().upper()
        name = (parsed.get("name") or "").strip()
        if not name:
            raise ValueError("빈 이름 응답")
        return {"decision": decision if decision in ("CREATE", "UPDATE") else "CREATE", "name": name}
    except Exception as e:
        handle_error("스킬식별판단", e)
        return {"decision": "CREATE", "name": (artifact_text or "새 스킬")[:40].strip()}


async def resolve_dmn_identity(artifact: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """DMN_RULE target이 CREATE(새 DMN)인지 UPDATE(기존 DMN)인지, 어떤 id/name을 쓸지 결정한다.

    candidates는 특정 에이전트가 소유한 기존 DMN 목록({"id","name","description"}) —
    이름+설명 전체를 LLM에 보여주고 판단하게 한다(progressive disclosure, 유사도 검색 아님).
    실패 시 CREATE + artifact의 decision.name으로 폴백한다(항상 결과를 반환).
    """
    llm = create_llm(streaming=False, temperature=0)

    decision_info = artifact.get("decision") or {}
    artifact_name = (decision_info.get("name") or "").strip()
    artifact_desc = (decision_info.get("description") or "").strip()

    candidates_text = "\n".join(
        f"- id: {c.get('id', '')}, 이름: {c.get('name', '')}, 설명: {c.get('description', '')}"
        for c in candidates
    ) or "없음"

    prompt = f"""
아래 새 DMN 규칙 제안이 이 에이전트가 이미 가지고 있는 DMN 규칙 중 하나를 고치는 것인지,
새 DMN 규칙이 필요한지 판단하세요.

**제안된 DMN 규칙:**
이름: {artifact_name}
설명: {artifact_desc}

**이 에이전트의 기존 DMN 규칙 목록:**
{candidates_text}

**판단 기준:**
- 기존 규칙 중 하나와 판단 대상/범위가 명확히 겹치면 UPDATE, 그 규칙의 정확한 id를 그대로 쓰세요.
- 겹치는 기존 규칙이 없으면 CREATE로 응답하세요 (id는 비워둠).

**응답 형식(JSON만):**
{{"decision": "CREATE 또는 UPDATE", "id": "UPDATE일 때 기존 id, CREATE면 빈 문자열", "name": "규칙 이름"}}
"""

    try:
        response = await llm.ainvoke(prompt)
        parsed = json.loads(clean_json_response(response.content))
        decision = (parsed.get("decision") or "CREATE").strip().upper()
        rid = (parsed.get("id") or "").strip() or None
        name = (parsed.get("name") or artifact_name or "새 DMN 규칙").strip()
        if decision == "UPDATE" and not rid:
            decision = "CREATE"
        return {"decision": decision if decision in ("CREATE", "UPDATE") else "CREATE", "id": rid, "name": name}
    except Exception as e:
        handle_error("DMN식별판단", e)
        return {"decision": "CREATE", "id": None, "name": artifact_name or "새 DMN 규칙"}
