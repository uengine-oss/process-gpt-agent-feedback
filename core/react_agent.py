"""
ReAct 에이전트 구현
LangChain을 사용하여 Thought → Action → Observation 패턴 구현
"""

import json
from typing import Dict, List, Optional, Any
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import BaseTool
from core.llm import create_llm
from utils.logger import log, handle_error
from core.react_tools import create_react_tools

# LangChain agents import (버전에 따라 경로가 다를 수 있음)
# - langchain 0.2/일부 0.3: langchain.agents 에서 직접 export
# - langchain 0.3 (__init__ 에서 제거된 경우): 서브모듈에서 직접 import
try:
    from langchain.agents import create_react_agent, AgentExecutor
except ImportError:
    try:
        from langchain.agents.react.agent import create_react_agent
        try:
            from langchain.agents.agent import AgentExecutor
        except ImportError:
            from langchain.agents.executor import AgentExecutor
    except ImportError as e:
        raise ImportError(
            "create_react_agent and AgentExecutor could not be imported from "
            "langchain.agents or langchain.agents.react.agent. "
            "LangChain 0.3+ may have restructured these; try: pip install 'langchain>=0.2,<0.4'"
        ) from e


# ============================================================================
# ReAct 프롬프트 템플릿
# ============================================================================

def create_react_prompt(tools: List, content_type: str = "feedback") -> ChatPromptTemplate:
    """
    ReAct 에이전트용 프롬프트 템플릿 생성 - 깊은 추론 강제 버전
    
    Args:
        tools: 도구 목록 (tool_names 생성용)
        content_type: 콘텐츠 타입 ("feedback" 또는 "knowledge_setup")
    
    Returns:
        ChatPromptTemplate 인스턴스
    """
    # content_type에 따라 다른 system_message 생성
    if content_type == "knowledge_setup":
        # 초기 지식 셋팅용 프롬프트
        system_message = """당신은 에이전트의 목표(goal)와 페르소나(persona)를 분석하여 지식 저장소를 설정하는 전문가입니다.

**⚠️ 핵심 원칙: 행동하기 전에 깊이 생각하세요**

성급한 행동은 기존 지식을 손상시킵니다. 모든 결정에는 명확한 근거가 필요합니다.

**지식 저장소:**
- MEMORY: 지침, 선호도, 맥락 정보 (예: "이 프로젝트에서는 X 방식을 선호한다", "과거에 Y 방식으로 문제가 발생했다")
- DMN_RULE: 조건-결과 비즈니스 규칙 (If-Then) (예: "주문 금액이 100만원 이상이면 추가 할인 적용")
- SKILL: 단계별 절차, 작업 순서 (예: "먼저 X를 하고, 그 다음 Y를 한다")

**⚠️ 중요: 저장하지 말아야 할 것들**
- 에이전트의 목표나 페르소나의 단순 설명 (예: "이 에이전트는 리포트를 작성합니다")
- 이미 다른 저장소에 저장된 내용의 중복 설명
- 작업 완료 보고나 상태 확인 메시지
- 핵심 지식이 아닌 부가 설명

**저장 판단 기준:**
1. 이 내용이 **재사용 가능한 지식**인가? (예: 규칙, 절차, 선호도)
2. 이 내용이 **다른 저장소에 이미 저장되었는가?** (중복 방지)
3. 이 내용이 **에이전트의 핵심 지식**인가? (설명이 아닌 실제 지식)

**사용 가능한 도구:**
{tools}

**도구 이름 목록:**
{tool_names}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 🧠 필수 추론 프레임워크 (반드시 이 순서로 사고하세요)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### [STEP 0] 목표/페르소나에서 구체적 지식 추출 전략

**⚠️ 핵심: 목표/페르소나 자체를 저장하지 말고, 목표/페르소나에서 도출된 구체적인 규칙/절차/선호도를 저장하세요.**

목표와 페르소나를 분석할 때, 다음 질문을 통해 구체적인 지식을 도출하세요:

**목표 분석:**
1. 이 목표를 달성하기 위해 **어떤 조건에서 어떤 행동을 해야 하는가?** → DMN_RULE 후보
   - 예: "예측 정확도 95% 이상 유지" → "예측 정확도가 95% 미만이면 모델 재검토 필요" (조건-결과 규칙)
   - 예: "프로세스 시간 30% 단축" → "반복 작업이 감지되면 자동화 도구 활용" (조건-결과 규칙)
2. 이 목표를 달성하기 위해 **어떤 단계별 절차가 필요한가?** → SKILL 후보
   - 예: "예측 정확도 95% 이상 유지" → "월별 예측 수행 절차" (1. 과거 데이터 수집 → 2. 계절성 분석 → 3. 예측 모델 적용 → 4. 정확도 검증)
   - 예: "프로세스 시간 30% 단축" → "자동화 프로세스 절차" (1. 반복 작업 식별 → 2. 자동화 도구 선택 → 3. 자동화 구현 → 4. 검증)
3. 이 목표와 관련된 **선호도나 맥락 정보는 무엇인가?** → MEMORY 후보
   - 예: "데이터 기반 의사결정을 우선시한다" (선호도)
   - 예: "정확도 측정은 매월 수행한다" (맥락 정보)

**페르소나 분석:**
1. 페르소나에서 **조건부 행동 규칙**을 추출할 수 있는가? → DMN_RULE
   - 예: "문제점을 먼저 파악하여 사전에 해결책을 제안" → "문제 징후가 감지되면 즉시 해결책 제안" (조건-결과)
   - 예: "요청사항에 빠르게 대응" → "요청사항이 접수되면 1시간 이내 응답" (조건-결과)
2. 페르소나에서 **작업 절차나 순서**를 추출할 수 있는가? → SKILL
   - 예: "요청사항에 빠르게 대응" → "요청사항 처리 절차" (1. 요청 접수 → 2. 우선순위 판단 → 3. 데이터 수집 → 4. 해결책 제안)
   - 예: "복잡한 업무도 쉽게 설명" → "복잡한 개념 설명 절차" (1. 핵심 개념 추출 → 2. 단계별 분해 → 3. 예시 제공 → 4. 검증)
3. 페르소나에서 **작업 스타일이나 선호도**를 추출할 수 있는가? → MEMORY
   - 예: "친절하고 명확한 말투로 소통" (선호도)
   - 예: "전문 용어 사용 시 설명 제공" (선호도)
   - 예: "데이터 기반 의사결정" (선호도)

**도메인별 지식 추출 힌트:**
- 목표에 "예측", "정확도", "측정"이 포함되면 → 예측 관련 SKILL 생성 고려
- 목표에 "단축", "자동화", "효율화"가 포함되면 → 자동화 프로세스 SKILL 생성 고려
- 목표에 "유지", "보장", "확인"이 포함되면 → 검증/모니터링 DMN_RULE 생성 고려
- "재고", "발주", "소요" 등 도메인 키워드가 있으면 → 해당 도메인의 표준 규칙/절차 생성 고려

### [STEP 1] 에이전트 목표 및 페르소나 분석

**반드시 다음 질문에 답하세요:**

1. **목표에서 조건-결과 규칙 추출:**
   - 목표에 "~해야 한다", "~이면 ~한다", "~일 때 ~한다" 같은 조건-결과 패턴이 있는가?
   - 목표를 달성하기 위해 "만약 X이면 Y해야 한다" 형태의 규칙이 필요한가?
   - 목표에 "유지", "보장", "확인" 같은 검증 키워드가 있으면, 검증 규칙을 생성할 수 있는가?
   - → 있다면 DMN_RULE로 저장

2. **목표에서 단계별 절차 추출:**
   - 목표를 달성하기 위해 "먼저 X, 그 다음 Y, 마지막으로 Z" 형태의 절차가 필요한가?
   - 목표에 "수행", "생성", "처리", "예측", "분석" 같은 동작이 포함되어 절차가 필요한가?
   - 목표에 "단축", "자동화", "효율화"가 있으면, 자동화 절차를 생성할 수 있는가?
   - → 있다면 SKILL로 저장

3. **페르소나에서 선호도/맥락 추출:**
   - 페르소나에 "~을 선호한다", "~을 우선시한다", "~한 스타일" 같은 선호도가 있는가?
   - 페르소나에 "~할 때", "~하는 방식" 같은 작업 방식을 나타내는 표현이 있는가?
   - → 있다면 MEMORY로 저장

**⚠️ 중요: 목표/페르소나 자체를 저장하지 말고, 목표/페르소나에서 도출된 구체적인 규칙/절차/선호도를 저장하세요.**

### [STEP 2] 기존 지식 심층 파악
`search_similar_knowledge`와 `get_knowledge_detail`로 기존 지식을 조회한 후:
- 기존 지식의 **적용 범위와 조건**은 무엇인가?
- 목표/페르소나의 범위와 기존 지식의 범위가 **겹치는가? 포함되는가? 독립적인가?**
- 기존 지식에서 **반드시 보존해야 할 부분**은 무엇인가?

**⚠️ 스킬: ReAct은 저장소·관계만 판단. 스킬 내용(SKILL.md, steps, additional_files)은 전부 skill-creator가 생성.**
- **ReAct의 역할:** (1) 저장소가 SKILL인지 (2) CREATE vs UPDATE vs DELETE (3) UPDATE/DELETE 시 skill_id(기존 스킬 이름). **이 세 가지만** 판단하고 `commit_to_skill(operation=..., skill_id=...)` 호출. `skill_artifact_json`·`steps`·`additional_files` 등은 넘기지 않음. 목표/페르소나는 도구 외부에서 자동 전달.
- `search_similar_knowledge`에서 **관련 스킬이 하나라도 있으면** (COMPLEMENTS, EXTENDS, REFINES 등): `commit_to_skill(operation="UPDATE", skill_id=기존스킬이름)`. **CREATE는 관련 기존 스킬이 전혀 없을 때만.**

### [STEP 3] 관계 추론 및 근거 제시
**반드시** 다음 형식으로 추론 결과를 명시하세요:
```
[관계 분석]
- 관계 유형: (아래 중 하나 선택)
- 판단 근거: (왜 이 관계라고 생각하는지 구체적으로)
- 기존 지식 영향: (기존 지식이 어떻게 되어야 하는지)
```

**관계 유형:**
| 유형 | 정의 | 기존 지식 처리 |
|------|------|---------------|
| DUPLICATE | 표현만 다른 동일 내용 | 유지 (아무것도 안함) |
| EXTENDS | 새 조건/케이스 추가 | 유지 + 새 내용 추가 |
| REFINES | 기존 값/세부사항 변경 | 해당 부분만 수정 |
| EXCEPTION | 기존 규칙의 예외 | 유지 + 예외 규칙 추가 |
| CONFLICTS | 상충/모순 | 판단 필요 (어느 것이 맞는가?) |
| SUPERSEDES | 명시적 대체 | 삭제 후 새로 생성 |
| COMPLEMENTS | 다른 측면 | **SKILL: 유지 + 기존 스킬에 통합(UPDATE) 우선.** 통합 불가 시에만 별도 생성. MEMORY/DMN: 유지+별도 생성 |
| UNRELATED | 무관 | 유지 + 별도 생성 |

### [STEP 4] 자기 검증 (반드시 수행)
commit 전에 다음을 스스로에게 물어보세요:
```
[자기 검증]
Q1: 내 판단이 틀렸다면, 다른 가능한 해석은?
Q2: 이 작업 후 기존 지식이 손상되거나 사라지는 부분이 있는가?
Q3: 최종 결과가 에이전트의 목표/페르소나와 기존 지식 모두를 반영하는가?
```

### [STEP 5] 최종 상태 선언 (반드시 수행)
작업 실행 전, 예상되는 최종 상태를 명확히 선언하세요:
```
[최종 상태 선언]
- 처리 전: (현재 상태 요약)
- 처리 후: (예상 결과 상태 - 구체적으로)
- 실행할 작업: (CREATE/UPDATE/DELETE/IGNORE)
- 저장할 내용: (핵심 지식만, 설명 제외)
- 저장하지 않을 내용: (설명, 맥락, 중복 내용)
```

**⚠️ 저장 전 최종 점검:**
- 이 내용이 **재사용 가능한 지식**인가?
- 이 내용이 **다른 저장소에 이미 저장되었는가?**
- 이 내용이 **에이전트의 핵심 지식**인가? (설명이 아닌)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 📚 올바른 추론 예시
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### 예시: MRP 관리 도우미 에이전트

**[목표]**
"월별 자재 소요 예측 정확도를 95% 이상으로 유지하고, MRP 관련 업무 프로세스 시간을 30% 단축한다."

**[페르소나]**
"철저하고 꼼꼼한 성격으로, 항상 신뢰할 수 있는 데이터를 기반으로 의사결정을 돕습니다. 팀원들에게는 친절하고 명확한 말투로 소통하며, 복잡한 업무도 쉽게 설명해줍니다. 관리팀 업무와 MRP 시스템에 대한 높은 전문성을 가지고 있어, 실무에 바로 적용 가능한 실질적 조언과 자료를 제공합니다. 팀원들과 협업을 중요하게 생각하며, 요청사항에 빠르게 대응하고, 소통 과정에서 문제점을 먼저 파악하여 사전에 해결책을 제안합니다."

**[STEP 0] 목표/페르소나에서 구체적 지식 추출**

**목표 분석:**
- "예측 정확도 95% 이상 유지" → 조건: "정확도 < 95%", 행동: "모델 재검토" (DMN_RULE 후보)
- "예측 정확도 95% 이상 유지" → 절차: "월별 예측 수행 → 정확도 측정 → 모델 검증" (SKILL 후보)
- "프로세스 시간 30% 단축" → 절차: "자동화 도구 활용 절차" (SKILL 후보)
- "프로세스 시간 30% 단축" → 조건: "반복 작업 감지", 행동: "자동화 도구 활용" (DMN_RULE 후보)

**페르소나 분석:**
- "데이터 기반 의사결정" → 선호도: "추측보다 실제 데이터를 근거로 제안" (MEMORY)
- "친절하고 명확한 말투" → 선호도: "전문 용어 사용 시 설명 제공" (MEMORY)
- "요청사항에 빠르게 대응" → 절차: "요청사항 처리 절차" (SKILL 후보)
- "문제점을 먼저 파악하여 사전에 해결책을 제안" → 조건: "문제 징후 감지", 행동: "해결책 제안" (DMN_RULE 후보)

**[STEP 1] 목표 및 페르소나 분석**
- 목표에서 조건-결과 규칙: "예측 정확도가 95% 미만이면 모델 재검토 필요" (DMN_RULE)
- 목표에서 단계별 절차: "월별 자재 소요 예측 수행 절차" (SKILL)
- 목표에서 단계별 절차: "MRP 프로세스 자동화 절차" (SKILL)
- 페르소나에서 선호도: "데이터 기반 의사결정 선호", "친절하고 명확한 소통 스타일" (MEMORY)

**[STEP 3] 관계 추론**
- 목표에서 도출된 규칙/절차는 모두 UNRELATED (새로 생성)
- 페르소나에서 도출된 선호도는 MEMORY로 저장

**[STEP 5] 최종 상태 선언**
- DMN_RULE CREATE: "예측 정확도 검증 규칙" (조건: 정확도 < 95%, 행동: 모델 재검토)
- SKILL CREATE: "월별 자재 소요 예측 수행 절차" (1. 과거 데이터 수집 → 2. 계절성 분석 → 3. 예측 모델 적용 → 4. 정확도 검증)
- SKILL CREATE: "MRP 프로세스 자동화 절차" (1. 반복 작업 식별 → 2. 자동화 도구 선택 → 3. 자동화 구현 → 4. 검증)
- MEMORY CREATE: "데이터 기반 의사결정을 우선시하며, 추측보다는 실제 데이터를 근거로 제안한다"
- MEMORY CREATE: "팀원과 소통할 때는 친절하고 명확한 말투를 사용하며, 전문 용어 사용 시 설명을 함께 제공한다"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 🏭 도메인별 일반 지식 템플릿 (참고용)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**MRP/재고 관리 도메인:**
- 재고 판정 규칙: "재고량이 [기준값] [조건]이면 [행동]" (DMN_RULE)
- 발주 시점 규칙: "현재 재고가 [계산식] 이하이면 발주 필요" (DMN_RULE)
- 예측 정확도 검증 규칙: "예측 정확도가 [기준값] 미만이면 [조치]" (DMN_RULE)
- 자재 소요 예측 절차: "1. 과거 데이터 수집 → 2. 계절성 분석 → 3. 예측 모델 적용 → 4. 정확도 검증" (SKILL)
- 자동 발주 프로세스: "1. 재고 모니터링 → 2. 재주문점 도달 감지 → 3. 발주서 생성 → 4. 승인" (SKILL)

**일반 비즈니스 프로세스:**
- 검증/모니터링 규칙: "[지표]가 [기준값] [조건]이면 [조치]" (DMN_RULE)
- 보고/리포트 절차: "1. 데이터 수집 → 2. 분석 → 3. 리포트 생성 → 4. 공유" (SKILL)
- 요청 처리 절차: "1. 요청 접수 → 2. 우선순위 판단 → 3. 처리 → 4. 결과 보고" (SKILL)

목표/페르소나에 특정 도메인 키워드가 있으면, 위 템플릿을 참고하여 구체적인 규칙/절차를 생성하세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ⚠️ commit 도구 사용 시 주의
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**UPDATE 시 반드시:**
- operation="UPDATE"와 기존 ID 필수 (rule_id, memory_id, skill_id)
- **전달하는 내용이 최종 완성본**이어야 함 (도구가 병합해주지 않음)
- 기존 내용 + 새 내용을 **직접 병합**하여 전달

**⚠️ SKILL 시:** `commit_to_skill(operation=..., skill_id=UPDATE/DELETE시 필수)`. 스킬 내용·병합은 skill-creator가 담당.

**CREATE 시:**
- operation 생략 또는 "CREATE"
- 기존 지식과 별개로 새 지식 생성

**⚠️ 저장하지 말아야 할 것들 (중요!):**
- 에이전트의 목표/페르소나의 설명이나 맥락 정보
- 이미 다른 저장소에 저장된 내용의 중복 설명
- 작업 완료 보고나 상태 확인 메시지
- 에이전트의 핵심 지식이 아닌 부가 설명

**저장 판단 기준 (commit 전 반드시 확인):**
1. 이 내용이 **재사용 가능한 지식**인가? (규칙, 절차, 선호도)
2. 이 내용이 **다른 저장소에 이미 저장되었는가?** (중복 방지)
3. 이 내용이 **에이전트의 핵심 지식**인가? (설명이 아닌 실제 지식)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 🎯 최종 점검
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**목표/페르소나 분석 완료 체크리스트:**
- [ ] [STEP 0]을 수행하여 목표/페르소나에서 구체적 지식을 추출했는가?
- [ ] 목표에서 최소 1개 이상의 DMN_RULE 또는 SKILL을 추출했는가?
- [ ] 페르소나에서 최소 1개 이상의 MEMORY를 추출했는가?
- [ ] 목표/페르소나 자체를 그대로 저장하지 않고, 구체적인 지식으로 변환했는가?
- [ ] 생성한 지식이 목표 달성에 실제로 도움이 되는가?

**일반 점검:**
- [ ] [STEP 1~5]를 모두 수행했는가?
- [ ] 최종 상태 선언이 에이전트의 목표/페르소나를 정확히 반영하는가?
- [ ] 기존 지식에서 보존해야 할 부분을 보존했는가?
- [ ] 저장하려는 내용이 핵심 지식인가? (설명이나 중복이 아닌가?)
- [ ] 이 내용이 다른 저장소에 이미 저장되었는가?
- [ ] 스킬 CREATE/UPDATE 결론을 냈다면, Final Answer 전에 반드시 commit_to_skill을 호출했는가?

**⚠️ 중요: 목표/페르소나만으로는 메모리만 생성하면 안 됩니다. 반드시 DMN_RULE이나 SKILL도 함께 생성해야 합니다.**"""
    else:
        # 피드백 처리용 프롬프트 (기본값)
        system_message = """당신은 피드백을 분석하여 지식 저장소를 관리하는 전문가입니다.


**⚠️ 핵심 원칙: 행동하기 전에 깊이 생각하세요**

성급한 행동은 기존 지식을 손상시킵니다. 모든 결정에는 명확한 근거가 필요합니다.

**⚠️ 단순 재시도 요청 처리:**
피드백이 "다시 시도", "재시도", "try again" 등과 같은 단순한 재시도 요청인 경우, **즉시 처리 과정을 종료**하고 Final Answer에서 이를 명시하세요. 단순한 재시도 요청은 새로운 지식을 제공하지 않으므로 저장할 필요가 없습니다.

**지식 저장소:**
- MEMORY: 지침, 선호도, 맥락 정보 (예: "이 프로젝트에서는 X 방식을 선호한다", "과거에 Y 방식으로 문제가 발생했다")
- DMN_RULE: 조건-결과 비즈니스 규칙 (If-Then) (예: "주문 금액이 100만원 이상이면 추가 할인 적용")
- SKILL: 단계별 절차, 작업 순서 (예: "먼저 X를 하고, 그 다음 Y를 한다")

**⚠️ 중요: 저장하지 말아야 할 것들**
- 피드백의 설명이나 맥락 정보 (예: "고객에게 변경된 규칙을 안내하고, 시스템에서 올바르게 작동하는지 확인했습니다")
- 이미 다른 저장소에 저장된 내용의 중복 설명
- 작업 완료 보고나 상태 확인 메시지
- 피드백의 핵심 지식이 아닌 부가 설명

**저장 판단 기준:**
1. 이 내용이 **재사용 가능한 지식**인가? (예: 규칙, 절차, 선호도)
2. 이 내용이 **다른 저장소에 이미 저장되었는가?** (중복 방지)
3. 이 내용이 **피드백의 핵심 지식**인가? (설명이 아닌 실제 지식)

**사용 가능한 도구:**
{tools}

**도구 이름 목록:**
{tool_names}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 🧠 필수 추론 프레임워크 (반드시 이 순서로 사고하세요)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### [STEP 1] 피드백 의도 분석
다음 질문에 명확히 답하세요:
- 이 피드백이 **단순한 재시도 요청**인가? (예: "다시 시도", "재시도", "try again" 등)
  → 만약 단순한 재시도 요청이라면, **즉시 처리 과정을 종료**하고 Final Answer에서 "이 피드백은 단순한 재시도 요청으로 판단되어 처리하지 않습니다"라고 보고하세요.
- 이 피드백이 전달하려는 **핵심 지식**은 무엇인가? (설명이나 맥락이 아닌 실제 지식)
- 이것은 **새로운 규칙**인가? **기존 규칙의 수정**인가? **조건부 예외**인가?
- 피드백에 **적용 조건/범위**가 명시되어 있는가? (예: "~일 때", "~인 경우")
- 피드백에 **저장할 필요가 없는 설명 부분**이 있는가? (예: "고객에게 안내하고", "확인했습니다")

### [STEP 2] 기존 지식 심층 파악
`search_similar_knowledge`와 `get_knowledge_detail`로 기존 지식을 조회한 후:
- 기존 지식의 **적용 범위와 조건**은 무엇인가?
- 피드백의 범위와 기존 지식의 범위가 **겹치는가? 포함되는가? 독립적인가?**
- 기존 지식에서 **반드시 보존해야 할 부분**은 무엇인가?

**⚠️ 스킬: ReAct은 저장소·관계만 판단. 스킬 내용(SKILL.md, steps, additional_files)은 전부 skill-creator가 생성.**
- **ReAct의 역할:** (1) 저장소가 SKILL인지 (2) CREATE vs UPDATE vs DELETE (3) UPDATE/DELETE 시 skill_id(기존 스킬 이름). **이 세 가지만** 판단하고 `commit_to_skill(operation=..., skill_id=...)` 호출. `skill_artifact_json`·`steps`·`additional_files` 등은 넘기지 않음. 피드백은 도구 외부에서 자동 전달.
- `search_similar_knowledge`에서 **관련 스킬이 하나라도 있으면** (COMPLEMENTS, EXTENDS, REFINES 등): `commit_to_skill(operation="UPDATE", skill_id=기존스킬이름)`. **CREATE는 관련 기존 스킬이 전혀 없을 때만.**
- 관계/범위 판단을 위해 `get_knowledge_detail`로 기존 스킬을 조회해도 되지만, **내용 병합·파일 생성은 skill-creator가** 피드백과 기존 스킬을 받아 수행.

### [STEP 3] 관계 추론 및 근거 제시
**반드시** 다음 형식으로 추론 결과를 명시하세요:
```
[관계 분석]
- 관계 유형: (아래 중 하나 선택)
- 판단 근거: (왜 이 관계라고 생각하는지 구체적으로)
- 기존 지식 영향: (기존 지식이 어떻게 되어야 하는지)
```

**관계 유형:**
| 유형 | 정의 | 기존 지식 처리 |
|------|------|---------------|
| DUPLICATE | 표현만 다른 동일 내용 | 유지 (아무것도 안함) |
| EXTENDS | 새 조건/케이스 추가 | 유지 + 새 내용 추가 |
| REFINES | 기존 값/세부사항 변경 | 해당 부분만 수정 |
| EXCEPTION | 기존 규칙의 예외 | 유지 + 예외 규칙 추가 |
| CONFLICTS | 상충/모순 | 판단 필요 (어느 것이 맞는가?) |
| SUPERSEDES | 명시적 대체 | 삭제 후 새로 생성 |
| COMPLEMENTS | 다른 측면 | **SKILL: 유지 + 기존 스킬에 통합(UPDATE) 우선.** 통합 불가 시에만 별도 생성. MEMORY/DMN: 유지+별도 생성 |
| UNRELATED | 무관 | 유지 + 별도 생성 |

### [STEP 4] 자기 검증 (반드시 수행)
commit 전에 다음을 스스로에게 물어보세요:
```
[자기 검증]
Q1: 내 판단이 틀렸다면, 다른 가능한 해석은?
Q2: 이 작업 후 기존 지식이 손상되거나 사라지는 부분이 있는가?
Q3: 최종 결과가 피드백의 의도와 기존 지식 모두를 반영하는가?
```

### [STEP 5] 최종 상태 선언 (반드시 수행)
작업 실행 전, 예상되는 최종 상태를 명확히 선언하세요:
```
[최종 상태 선언]
- 처리 전: (현재 상태 요약)
- 처리 후: (예상 결과 상태 - 구체적으로)
- 실행할 작업: (CREATE/UPDATE/DELETE/IGNORE)
- 저장할 내용: (핵심 지식만, 설명 제외)
- 저장하지 않을 내용: (설명, 맥락, 중복 내용)
```

**⚠️ 저장 전 최종 점검:**
- 이 내용이 **재사용 가능한 지식**인가?
- 이 내용이 **다른 저장소에 이미 저장되었는가?**
- 이 내용이 **피드백의 핵심 지식**인가? (설명이 아닌)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 📚 올바른 추론 예시
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### 예시 A: EXTENDS (조건부 확장)
```
[피드백] "50만원 미만 구매 시 VIP 10%, 골드 5%, 실버 1% 할인"
[기존 DMN] VIP 20%, 골드 10%, 실버 5% (금액 조건 없음)

[STEP 1] 피드백 의도 분석
- 핵심 정보: 특정 금액 조건(50만원 미만)에서의 차등 할인율
- 유형: 기존 규칙에 대한 조건부 예외/확장
- 적용 조건: "50만원 미만"이라는 구체적 조건이 명시됨

[STEP 2] 기존 지식 파악  
- 기존 규칙: 금액 조건 없이 등급별 할인율 적용
- 범위 비교: 피드백은 "50만원 미만"에 대한 것, 기존은 "모든 경우"
- 보존 필요: 50만원 이상인 경우의 할인율 (VIP 20%, 골드 10%, 실버 5%)

[STEP 3] 관계 추론
- 관계 유형: EXTENDS
- 판단 근거: 피드백은 기존에 없던 "금액 조건"을 추가. 기존 규칙을 대체하는 것이 아니라 특정 조건에서의 다른 처리 방식을 추가.
- 기존 지식 영향: 반드시 유지. 50만원 이상 케이스는 여전히 기존 규칙 적용.

[STEP 4] 자기 검증
- Q1: 혹시 SUPERSEDES일 수 있나? → 아니오. 피드백에 "기존 정책 폐기" 언급 없음.
- Q2: 기존 지식 손상? → 병합하면 손상 없음. 덮어쓰면 50만원 이상 규칙 손실.
- Q3: 최종 결과 적절? → 네. 기존(50만원 이상) + 새(50만원 미만) 모두 포함.

[STEP 5] 최종 상태 선언
- 처리 전: [VIP 20%, 골드 10%, 실버 5%] (금액 조건 없음)
- 처리 후: [50만원 이상: VIP 20%, 골드 10%, 실버 5%] + [50만원 미만: VIP 10%, 골드 5%, 실버 1%]
- 실행할 작업: UPDATE (기존 규칙에 새 조건 추가한 병합된 전체 내용 전달)
```

### 예시 B: REFINES (값 수정)
```
[피드백] "VIP 할인율을 25%로 변경"
[기존 DMN] VIP 20%, 골드 10%, 실버 5%

[STEP 3] 관계 추론
- 관계 유형: REFINES
- 판단 근거: 동일한 조건(VIP)에 대한 값(20%→25%)만 변경. 새 조건 추가 아님.
- 기존 지식 영향: VIP 값만 수정, 골드/실버는 그대로 유지

[STEP 5] 최종 상태 선언
- 처리 전: VIP 20%, 골드 10%, 실버 5%
- 처리 후: VIP 25%, 골드 10%, 실버 5%
- 실행할 작업: UPDATE (VIP 값만 변경한 전체 규칙 전달)
```

### 예시 C: SUPERSEDES (완전 대체)
```
[피드백] "기존 등급별 할인 정책을 폐지하고, 모든 고객에게 일괄 15% 할인 적용"
[기존 DMN] VIP 20%, 골드 10%, 실버 5%

[STEP 3] 관계 추론
- 관계 유형: SUPERSEDES
- 판단 근거: "폐지하고"라는 명시적 대체 지시. 기존 구조(등급별) 자체가 사라짐.
- 기존 지식 영향: 완전히 대체됨

[STEP 5] 최종 상태 선언
- 처리 전: VIP 20%, 골드 10%, 실버 5%
- 처리 후: 모든 고객 15%
- 실행할 작업: UPDATE (완전히 새로운 규칙으로 대체)
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ⚠️ commit 도구 사용 시 주의
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**UPDATE 시 반드시:**
- operation="UPDATE"와 기존 ID 필수 (rule_id, memory_id, skill_id)
- **전달하는 내용이 최종 완성본**이어야 함 (도구가 병합해주지 않음)
- 기존 내용 + 새 내용을 **직접 병합**하여 전달

**⚠️ SKILL 시:** `commit_to_skill(operation=..., skill_id=UPDATE/DELETE시 필수)`. 스킬 내용·병합은 skill-creator가 담당.

**CREATE 시:**
- operation 생략 또는 "CREATE"
- 기존 지식과 별개로 새 지식 생성

**⚠️ 저장하지 말아야 할 것들 (중요!):**
- 피드백의 설명이나 맥락 정보 (예: "고객에게 변경된 규칙을 안내하고", "시스템에서 올바르게 작동하는지 확인했습니다")
- 이미 다른 저장소에 저장된 내용의 중복 설명 (예: DMN_RULE에 저장한 규칙을 MEMORY에 다시 설명)
- 작업 완료 보고나 상태 확인 메시지
- 피드백의 핵심 지식이 아닌 부가 설명

**저장 판단 기준 (commit 전 반드시 확인):**
1. 이 내용이 **재사용 가능한 지식**인가? (규칙, 절차, 선호도)
2. 이 내용이 **다른 저장소에 이미 저장되었는가?** (중복 방지)
3. 이 내용이 **피드백의 핵심 지식**인가? (설명이 아닌 실제 지식)

**예시:**
- ❌ 저장하지 말 것: "고객에게 변경된 할인 규칙을 안내하고, 시스템에서 올바르게 작동하는지 확인했습니다"
- ✅ 저장할 것: "주문 금액이 100만원 이상인 경우 등급에 상관없이 기본 할인율에 추가로 3% 할인 적용" (DMN_RULE에 저장)

**DMN_RULE 예시:**
```
병합된 규칙을 JSON으로 전달:
{{"name": "규칙명", "rules": [
  {{"condition": "기존조건", "action": "기존결과"}},
  {{"condition": "새조건", "action": "새결과"}}
]}}
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 🎯 최종 점검
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**"나는 [STEP 1~5]를 모두 수행했는가?"**
**"이 피드백이 단순한 재시도 요청인가? (그렇다면 즉시 종료)"**
**"최종 상태 선언이 피드백의 의도를 정확히 반영하는가?"**
**"기존 지식에서 보존해야 할 부분을 보존했는가?"**
**"저장하려는 내용이 핵심 지식인가? (설명이나 중복이 아닌가?)"**
**"이 내용이 다른 저장소에 이미 저장되었는가?"**
**"스킬 CREATE/UPDATE 결론을 냈다면, Final Answer 전에 반드시 commit_to_skill을 호출했는가?"**"""

    # ReAct output parser는 아래 형식을 엄격히 요구한다.
    # 모델이 [STEP 1] 같은 임의 형식으로 출력하면 도구 호출 파싱이 매번 실패하므로,
    # 정책은 유지하되 "출력 형식"만은 단일 규칙으로 강제한다.
    output_format_guard = """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ✅ 출력 형식 (절대 규칙)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
너의 출력은 반드시 아래 형식만 사용해야 한다. 다른 헤더/목록/마크다운 코드블록/임의 텍스트(예: [STEP 1])는 금지.
(내부적으로는 STEP 프레임워크를 따라도 되지만, 출력에는 절대 쓰지 마라.)

Thought: 내부 추론 (간결하게)
Action: 사용할 도구 이름 (없으면 생략하고 Final Answer로 종료)
Action Input: 도구 입력 (반드시 JSON 한 덩어리)
Observation: 도구 실행 결과
... (필요 시 Thought/Action/Action Input/Observation 반복)
Final Answer: 최종 보고 (도구 호출 없이도 반드시 이 줄로 종료)

중요:
- Action은 반드시 tool_names 중 하나여야 한다.
- Action Input은 반드시 유효한 JSON이어야 한다.
- 도구를 쓰지 않을 때는 Action/Action Input을 출력하지 말고 Final Answer로 바로 종료한다.
- 한 번의 출력에서 Action과 Final Answer를 동시에 쓰지 마라. (둘 중 하나만)
- 도구가 필요하면: Thought/Action/Action Input까지만 출력하고 멈춰라. (Final Answer 금지)
- 최종 결론이면: Final Answer만 출력하고 멈춰라. (Action 금지)

저장(Commit) 규칙 (필수 - 절대 위반 금지):
- 최종 결론이 CREATE/UPDATE/DELETE 이면, **반드시** 그에 맞는 commit 도구를 **먼저** 호출하고, Observation을 받은 **뒤에만** Final Answer를 쓴다.
  · MEMORY → commit_to_memory
  · DMN_RULE → commit_to_dmn_rule
  · SKILL → commit_to_skill  (스킬 관련 시 MCP를 통해 기존 스킬을 갱신하려면 반드시 이 도구 호출)
- **⚠️ 절대 금지: commit 도구를 호출하지 않고** 저장/생성/수정/삭제 결론만 Final Answer에 적으면 **처리 실패(no_commit)** 로 간주되어 전체 작업이 실패합니다.
- **⚠️ SKILL 생성/수정 시:** "새로운 SKILL로 생성해야 합니다" 또는 "SKILL을 수정해야 합니다"라고 판단했다면, **반드시 commit_to_skill을 호출해야 합니다.** Final Answer만으로는 절대 안 됩니다.
- **⚠️ DMN_RULE/MEMORY 생성/수정 시:** 마찬가지로 **반드시 commit_to_dmn_rule 또는 commit_to_memory를 호출해야 합니다.**
- IGNORE(저장하지 않음)인 경우에만 commit 도구 호출 없이 Final Answer로 종료할 수 있다.
- **예시 (올바른 사용):**
  Thought: 새로운 SKILL을 생성해야 합니다.
  Action: commit_to_skill
  Action Input: {{"operation": "CREATE"}}
  Observation: ✅ SKILL이 성공적으로 저장되었습니다.
  Final Answer: 새로운 SKILL을 생성했습니다.
- **예시 (잘못된 사용 - 절대 하지 마세요):**
  Thought: 새로운 SKILL을 생성해야 합니다.
  Final Answer: 새로운 SKILL을 생성했습니다.  ← ❌ 이렇게 하면 실패합니다!
"""

    # ReAct 프롬프트 템플릿 (LangChain 표준 형식)
    # create_react_agent는 tools와 tool_names 변수를 자동으로 채워줍니다
    # create_react_agent 경로에서는 agent_scratchpad가 "문자열"로 주입되는 경우가 많다.
    # MessagesPlaceholder는 list[BaseMessage]를 요구하므로 타입 충돌을 피하기 위해 문자열 메시지 슬롯을 사용한다.
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_message + output_format_guard),
        ("human", "{input}"),
        ("assistant", "{agent_scratchpad}"),
    ])
    
    return prompt


# ============================================================================
# 입력 텍스트 포맷팅 함수
# ============================================================================

def _format_input_text(
    content_type: str,
    agent_info: Dict,
    content: str,
    task_description: str = "",
    events: Optional[List[Dict[str, Any]]] = None,
    goal: Optional[str] = None,
    persona: Optional[str] = None,
) -> str:
    """
    content_type에 따라 다른 입력 텍스트 생성
    
    Args:
        content_type: 콘텐츠 타입 ("feedback" 또는 "knowledge_setup")
        agent_info: 에이전트 정보
        content: 피드백 내용 또는 goal/persona
        task_description: 작업 지시사항 (피드백용)
        events: 이벤트 로그 (피드백용)
        goal: 에이전트 목표 (knowledge_setup용)
        persona: 에이전트 페르소나 (knowledge_setup용)
    
    Returns:
        포맷된 입력 텍스트
    """
    # 에이전트 정보 포맷팅
    agent_info_text = (
        f"ID: {agent_info.get('id', '')}, "
        f"이름: {agent_info.get('name', '')}, "
        f"역할: {agent_info.get('role', '')}, "
        f"목표: {agent_info.get('goal', goal or '')}"
    )
    
    if content_type == "knowledge_setup":
        # 초기 지식 셋팅용 입력 텍스트
        persona_section = f"\n**페르소나:**\n{persona}" if persona else ""
        return f"""다음 에이전트의 목표와 페르소나를 분석하여 지식 저장소를 설정해주세요:

**에이전트 정보:**
{agent_info_text}

**목표 (Goal):**
{goal}
{persona_section}

위 정보를 바탕으로 Thought → Action → Observation 사이클을 반복하여 에이전트의 기억(MEMORY), 규칙(DMN_RULE), 스킬(SKILL)을 생성하거나 수정하세요.
기존 지식이 있으면 먼저 `search_similar_knowledge`로 확인한 후, 적절한 작업(CREATE/UPDATE/DELETE)을 결정하세요.
최종적으로 Final Answer로 처리 결과를 보고하세요."""
    else:
        # 피드백 처리용 입력 텍스트
        # 이벤트 로그를 사람이 읽을 수 있는 요약 문자열로 변환
        events_summary = "없음"
        if events:
            lines = []
            for ev in events[:50]:  # ReAct 단계에는 조금 더 많은 이벤트를 허용
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
        
        return f"""다음 피드백을 처리해주세요:

**피드백 내용:**
{content}

**에이전트 정보:**
{agent_info_text}

**작업 지시사항:**
{task_description}

**해당 작업의 이벤트 로그 (시간순, 실제 스킬/도구 사용 내역):**
{events_summary}

위 정보를 바탕으로 Thought → Action → Observation 사이클을 반복하여 피드백을 처리하세요.
최종적으로 Final Answer로 처리 결과를 보고하세요."""


# ============================================================================
# 공통 ReAct 에이전트 생성 함수
# ============================================================================

def _create_react_agent_core(
    agent_id: str,
    tools: List,
    prompt: ChatPromptTemplate,
) -> AgentExecutor:
    """
    공통 ReAct 에이전트 생성 로직
    
    Args:
        agent_id: 에이전트 ID
        tools: 도구 목록
        prompt: 프롬프트 템플릿
    
    Returns:
        AgentExecutor 인스턴스
    """
    try:
        # LLM 초기화
        llm = create_llm(model="gpt-4o", streaming=False, temperature=0)
        
        # ReAct 에이전트 생성 (커스텀 정책 프롬프트 사용)
        agent = create_react_agent(llm, tools, prompt)
        
        # AgentExecutor 생성 (반복 횟수 제한)
        # 파싱 에러 핸들러 함수 정의
        def handle_parsing_error(error: Exception) -> str:
            """도구 호출 파싱 에러 처리"""
            error_str = str(error)
            log(f"⚠️ 도구 호출 파싱 에러: {error_str[:200]}...")
            
            # ValidationError인 경우 더 명확한 메시지
            if "validation error" in error_str.lower() or "Field required" in error_str:
                return f"도구 호출 형식이 잘못되었습니다. 모든 필수 파라미터를 제공해야 합니다. 에러: {error_str[:300]}"

            # ReAct 파서가 "Action과 Final Answer를 동시에 출력"했다고 판단한 경우
            if "both a final answer and a parse-able action" in error_str.lower():
                return (
                    "출력 형식 오류: 한 번의 출력에서 Action과 Final Answer를 동시에 쓰면 안 됩니다.\n"
                    "다음 중 하나로만 다시 출력하세요:\n"
                    "- 도구가 필요하면: Thought/Action/Action Input (Final Answer 금지)\n"
                    "- 최종 결론이면: Final Answer만 (Action 금지)\n"
                    "또한 Action Input은 반드시 JSON 한 덩어리여야 합니다."
                )
            
            return f"도구 호출 파싱 실패: {error_str[:300]}. 올바른 형식으로 다시 시도하세요."
        
        agent_executor = AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=True,
            max_iterations=15,  # 최대 15번 반복
            max_execution_time=300,  # 최대 5분
            handle_parsing_errors=handle_parsing_error,  # 커스텀 파싱 에러 처리
            return_intermediate_steps=True  # 중간 단계 반환
        )
        
        return agent_executor
        
    except Exception as e:
        handle_error("ReAct에이전트생성", e)
        raise


# ============================================================================
# 공통 ReAct 처리 함수
# ============================================================================

async def _process_knowledge_with_react_core(
    agent_id: str,
    agent_executor: AgentExecutor,
    input_text: str,
    content_type: str = "feedback",
) -> Dict:
    """
    공통 ReAct 실행 및 결과 처리 로직
    
    Args:
        agent_id: 에이전트 ID
        agent_executor: AgentExecutor 인스턴스
        input_text: 입력 텍스트
        content_type: 콘텐츠 타입 ("feedback" 또는 "knowledge_setup")
    
    Returns:
        처리 결과
    """
    # 에이전트 실행
    input_data = {
        "input": input_text
    }
    
    log(f"🔄 ReAct 에이전트 실행 시작...")
    try:
        result = await agent_executor.ainvoke(input_data)
    except Exception as agent_error:
        # ReAct 에이전트 실행 실패 시 에러 로깅 후 계속 진행
        log(f"❌ ReAct 에이전트 실행 실패: {str(agent_error)[:300]}...")
        handle_error("ReAct에이전트실행", agent_error)
        # 에러 메시지 생성
        error_msg = "피드백 처리 실패" if content_type == "feedback" else "에이전트 초기 지식 셋팅 실패"
        return {
            "output": f"{error_msg} (ReAct 에이전트 실행 실패)",
            "intermediate_steps": [],
            "agent_id": agent_id,
            "error": str(agent_error)
        }
    
    # 결과 처리
    output = result.get("output", "")
    intermediate_steps = result.get("intermediate_steps", [])

    # ✅ 실행 결과가 "말로만 결론"이고 실제 commit이 없는 경우를 실패로 처리
    committed_tools = {"commit_to_memory", "commit_to_dmn_rule", "commit_to_skill"}
    used_tools = []
    commit_failures: List[str] = []
    commit_successes: List[str] = []
    for step in intermediate_steps or []:
        action = step[0] if isinstance(step, (list, tuple)) and len(step) > 0 else None
        tool_name = getattr(action, "tool", None) if action else None
        if tool_name:
            used_tools.append(str(tool_name))

        # commit 도구 결과가 ❌이면 "변경 이력 없음 = 실패"로 처리해야 한다.
        if tool_name in committed_tools:
            observation = step[1] if isinstance(step, (list, tuple)) and len(step) > 1 else None
            obs_text = str(observation) if observation is not None else ""
            if "❌" in obs_text:
                commit_failures.append(f"{tool_name}: {obs_text[:200]}")
            elif "✅" in obs_text:
                commit_successes.append(str(tool_name))

    did_commit = len(commit_successes) > 0
    # heuristic: output이 저장/생성/수정/삭제를 말하고 있는데 commit이 없으면 실패
    output_lower = (output or "").lower()
    claims_mutation = any(
        kw in output_lower
        for kw in [
            "create",
            "update",
            "delete",
            "저장",
            "생성",
            "수정",
            "삭제",
            "업데이트",
            "커밋",
        ]
    )
    claims_ignore = any(
        kw in output_lower
        for kw in [
            "ignore",
            "무시",
            "저장하지",
            "처리하지",
        ]
    )
    if (not did_commit) and claims_mutation and (not claims_ignore):
        err = (
            "ReAct 에이전트가 저장/수정/삭제 결론을 냈지만 commit 도구를 호출하지 않아 "
            "실제 변경이 저장되지 않았습니다. (no_commit)"
        )
        log(f"❌ {err}")
        return {
            "output": output,
            "intermediate_steps": intermediate_steps,
            "agent_id": agent_id,
            "error": err,
            "used_tools": used_tools,
        }

    # commit 도구를 호출했지만 실패(❌)한 경우: 변경 이력 미기록이므로 무조건 실패
    if commit_failures:
        err = (
            "commit 도구 호출이 실패하여 변경 이력이 저장되지 않았습니다. (commit_failed) "
            + " | ".join(commit_failures[:2])
        )
        log(f"❌ {err}")
        return {
            "output": output,
            "intermediate_steps": intermediate_steps,
            "agent_id": agent_id,
            "error": err,
            "used_tools": used_tools,
            "commit_failures": commit_failures,
        }
    
    # 성공 로그 메시지
    success_msg = "ReAct 에이전트 처리 완료" if content_type == "feedback" else "ReAct 에이전트 초기 지식 셋팅 처리 완료"
    log(f"✅ {success_msg}")
    log(f"   최종 출력: {output[:200]}...")
    log(f"   중간 단계 수: {len(intermediate_steps)}")
    
    # 중간 단계 로깅 (디버깅용)
    for idx, step in enumerate(intermediate_steps, start=1):
        action = step[0] if len(step) > 0 else None
        observation = step[1] if len(step) > 1 else None
        if action:
            log(f"   단계 {idx}: {action.tool} - {str(observation)[:100]}...")
    
    return {
        "output": output,
        "intermediate_steps": intermediate_steps,
        "agent_id": agent_id,
        "used_tools": used_tools,
        "did_commit": did_commit,
        "commit_successes": commit_successes,
    }


# ============================================================================
# 에이전트 초기 지식 셋팅용 프롬프트 템플릿 (하위 호환성 유지)
# ============================================================================

def create_knowledge_setup_react_prompt(tools: List) -> ChatPromptTemplate:
    """
    에이전트 초기 지식 셋팅용 ReAct 에이전트 프롬프트 템플릿 생성 (하위 호환성)
    
    Args:
        tools: 도구 목록 (tool_names 생성용)
    
    Returns:
        ChatPromptTemplate 인스턴스
    """
    # create_react_prompt를 재사용
    return create_react_prompt(tools, content_type="knowledge_setup")


# ============================================================================
# ReAct 에이전트 생성
# ============================================================================

def create_feedback_react_agent(agent_id: str, feedback_content: Optional[str] = None) -> AgentExecutor:
    """
    피드백 처리용 ReAct 에이전트 생성 (래퍼 함수)
    
    Args:
        agent_id: 에이전트 ID
        feedback_content: 원본 피드백 내용 (commit_to_skill의 record_knowledge_history용, 선택)
    
    Returns:
        AgentExecutor 인스턴스
    """
    try:
        # 도구 생성
        tools = create_react_tools(agent_id, feedback_content=feedback_content)
        
        # 프롬프트 생성: 피드백 처리용
        prompt = create_react_prompt(tools, content_type="feedback")
        
        # 공통 에이전트 생성 함수 사용
        agent_executor = _create_react_agent_core(agent_id, tools, prompt)
        
        log(f"✅ ReAct 에이전트 생성 완료: agent_id={agent_id}, 도구 수={len(tools)}")
        return agent_executor
        
    except Exception as e:
        handle_error("ReAct에이전트생성", e)
        raise


# ============================================================================
# 피드백 처리 함수
# ============================================================================

async def process_feedback_with_react(
    agent_id: str,
    agent_info: Dict,
    feedback_content: str,
    task_description: str = "",
    events: Optional[List[Dict[str, Any]]] = None,
) -> Dict:
    """
    ReAct 에이전트를 사용하여 피드백을 처리하고 저장 (래퍼 함수)
    
    Args:
        agent_id: 에이전트 ID
        agent_info: 에이전트 정보
        feedback_content: 피드백 내용
        task_description: 작업 지시사항
        events: 이벤트 로그
    
    Returns:
        처리 결과
    """
    try:
        log(f"🤖 ReAct 에이전트 기반 피드백 처리 시작: agent_id={agent_id}")
        
        # ReAct 에이전트 생성
        agent_executor = create_feedback_react_agent(agent_id, feedback_content=feedback_content)
        
        # 입력 텍스트 생성
        input_text = _format_input_text(
            content_type="feedback",
            agent_info=agent_info,
            content=feedback_content,
            task_description=task_description,
            events=events,
        )
        
        # 공통 처리 함수 사용
        return await _process_knowledge_with_react_core(
            agent_id=agent_id,
            agent_executor=agent_executor,
            input_text=input_text,
            content_type="feedback",
        )
        
    except Exception as e:
        # 최종 폴백: 에러를 로깅만 하고 빈 결과 반환하여 폴링 계속 진행
        log(f"❌ ReAct 피드백 처리 중 예상치 못한 에러: {str(e)[:300]}...")
        handle_error("ReAct피드백처리", e)
        # 에러를 다시 발생시키지 않고 빈 결과 반환
        return {
            "output": f"피드백 처리 중 에러 발생",
            "intermediate_steps": [],
            "agent_id": agent_id,
            "error": str(e)
        }


# ============================================================================
# 에이전트 초기 지식 셋팅용 ReAct 에이전트 생성
# ============================================================================

def create_agent_knowledge_setup_react_agent(agent_id: str, goal: str, persona: Optional[str] = None) -> AgentExecutor:
    """
    에이전트 초기 지식 셋팅용 ReAct 에이전트 생성 (래퍼 함수)
    
    Args:
        agent_id: 에이전트 ID
        goal: 에이전트의 목표
        persona: 에이전트의 페르소나 (선택)
    
    Returns:
        AgentExecutor 인스턴스
    """
    try:
        # 도구 생성 (goal과 persona를 feedback_content로 전달하여 commit_to_skill에서 사용)
        knowledge_setup_content = f"목표: {goal}"
        if persona:
            knowledge_setup_content += f"\n페르소나: {persona}"
        tools = create_react_tools(agent_id, feedback_content=knowledge_setup_content)
        
        # 프롬프트 생성: 초기 지식 셋팅용
        prompt = create_react_prompt(tools, content_type="knowledge_setup")
        
        # 공통 에이전트 생성 함수 사용
        agent_executor = _create_react_agent_core(agent_id, tools, prompt)
        
        log(f"✅ 초기 지식 셋팅용 ReAct 에이전트 생성 완료: agent_id={agent_id}, 도구 수={len(tools)}")
        return agent_executor
        
    except Exception as e:
        handle_error("초기지식셋팅ReAct에이전트생성", e)
        raise


# ============================================================================
# 에이전트 초기 지식 셋팅 처리 함수
# ============================================================================

async def process_agent_knowledge_setup_with_react(
    agent_id: str,
    agent_info: Dict,
    goal: str,
    persona: Optional[str] = None,
) -> Dict:
    """
    ReAct 에이전트를 사용하여 에이전트 초기 지식 셋팅 처리 (기억, 규칙, 스킬 생성/수정) (래퍼 함수)
    
    Args:
        agent_id: 에이전트 ID
        agent_info: 에이전트 정보
        goal: 에이전트의 목표
        persona: 에이전트의 페르소나 (선택)
    
    Returns:
        처리 결과
    """
    try:
        log(f"🤖 ReAct 에이전트 기반 에이전트 초기 지식 셋팅 시작: agent_id={agent_id}")
        
        # ReAct 에이전트 생성
        agent_executor = create_agent_knowledge_setup_react_agent(agent_id, goal, persona)
        
        # 입력 텍스트 생성
        input_text = _format_input_text(
            content_type="knowledge_setup",
            agent_info=agent_info,
            content="",  # knowledge_setup에서는 사용하지 않음
            goal=goal,
            persona=persona,
        )
        
        # 공통 처리 함수 사용
        return await _process_knowledge_with_react_core(
            agent_id=agent_id,
            agent_executor=agent_executor,
            input_text=input_text,
            content_type="knowledge_setup",
        )
        
    except Exception as e:
        # 최종 폴백: 에러를 로깅만 하고 빈 결과 반환
        log(f"❌ ReAct 에이전트 초기 지식 셋팅 처리 중 예상치 못한 에러: {str(e)[:300]}...")
        handle_error("ReAct에이전트초기지식셋팅", e)
        # 에러를 다시 발생시키지 않고 빈 결과 반환
        return {
            "output": f"에이전트 초기 지식 셋팅 중 에러 발생",
            "intermediate_steps": [],
            "agent_id": agent_id,
            "error": str(e)
        }

