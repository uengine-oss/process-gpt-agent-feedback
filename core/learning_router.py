import json
import os
from typing import Dict, Optional
from core.llm import create_llm
from utils.logger import log, handle_error
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# 유틸리티 함수
# ============================================================================

def clean_json_response(content: str) -> str:
    """LLM 응답에서 백틱과 json 키워드 제거"""
    content = content.replace("```json", "").replace("```", "")
    return content.strip()

# ============================================================================
# 학습 라우팅
# ============================================================================

async def route_learning(candidate: Dict, existing_skill_content: Optional[str] = None) -> Dict:
    """
    학습 후보를 받아서 저장 대상(MEMORY / DMN_RULE / SKILL / MIXED)을 결정
    
    Args:
        candidate: {
            "content": "통합된 피드백 본문",
            "intent_hint": "판단 기준인지 / 절차인지 / 지침인지에 대한 요약 힌트"
        }
        existing_skill_content: 기존 스킬의 SKILL.md 전체 내용 (UPDATE 시 기존 내용 보존을 위해)
    
    Returns:
        {
            "target": "MEMORY | DMN_RULE | SKILL | MIXED",
            "artifacts": {
                "memory": "...",          # optional
                "dmn": {...},             # optional
                "skill": {...}            # optional
            }
        }
    """
    
    llm = create_llm(model="gpt-4o", streaming=False, temperature=0)
    
    content = candidate.get("content", "")
    intent_hint = candidate.get("intent_hint", "")
    
    # 기존 스킬 내용이 있으면 프롬프트에 포함
    existing_skill_section = ""
    if existing_skill_content:
        existing_skill_section = f"""

**⚠️ 기존 스킬 내용 (반드시 보존해야 함):**
다음은 업데이트할 기존 스킬의 전체 내용입니다. 이 내용의 구조, 섹션, 예시, 참고 문서 등을 모두 보존하면서 피드백 요구사항을 통합해야 합니다.

```markdown
{existing_skill_content}
```

**중요 지침:**
- 기존 스킬의 상세한 워크플로우, 예시, 참고 문서, 사용 시점 등 모든 섹션을 보존하세요.
- 피드백 요구사항은 기존 내용에 추가/개선하는 형태로 통합하세요.
- 기존 내용을 삭제하거나 단순화하지 마세요.
- overview, steps, usage, additional_files 모두에서 기존의 풍부한 내용을 유지하세요.
- **기존 파일 처리 규칙**:
  · 기존 스킬에 이미 `scripts/`, `docs/`, `references/` 폴더의 파일들이 있다면, 
    피드백 요구사항에 따라 해당 파일들을 수정하거나 새 파일을 추가하세요.
  · 기존 파일을 수정하는 경우: 해당 파일 경로와 수정된 전체 내용을 additional_files에 포함하세요.
  · 새 파일을 추가하는 경우: 새 파일 경로와 내용을 additional_files에 추가하세요.
  · Python 코드 파일은 `scripts/`에, Markdown 문서는 `references/`에 배치하세요.
  · 기존 파일들을 삭제하지 말고, 필요시 수정하거나 새 파일을 추가하세요.
"""

    prompt = f"""
다음 피드백을 분석하여 학습 유형을 분류하고 적절한 저장소를 결정해주세요.

**피드백 내용:**
{content}

**의도 힌트:**
{intent_hint}{existing_skill_section}

**지식 저장소 분류 기준:**

피드백 내용에 따라 다음 세 가지 저장소 중 하나로 분류되어야 합니다:

1. **MEMORY (기억) → mem0 저장소**
   - 지침, 선호도, 맥락적 주의사항, 경험 기반 정보
   - 개인적 선호나 프로젝트별 특성 정보
   - "이 프로젝트에서는 X 방식을 선호한다"
   - "과거에 Y 방식으로 문제가 발생했다"
   - "사용자는 간결한 설명을 선호한다"
   - 일반적인 가이드라인이나 맥락 정보
   - **저장소: mem0**에 저장됨

2. **DMN_RULE (의사결정 규칙) → DMN Rule 저장소**
   - 조건-결과 형태의 비즈니스 판단 규칙
   - "만약 X이면 Y해야 한다", "항상 X해야 한다", "반드시 X해야 한다"
   - 명확한 조건문과 그에 따른 행동 규칙
   - 예: "주문 금액이 100만원 이상이면 결제 승인을 받아야 한다"
   - 예: "항상 사용자에게 확인을 받아야 한다"
   - **저장소: DMN Rule**에 저장됨
   
3. **SKILL (실행 규칙) → Skills 저장소**
   - 반복 가능한 절차, 행동 방식, 작업 순서
   - "먼저 X를 하고, 그 다음 Y를 하고, 마지막으로 Z를 한다"처럼 단계가 분명한 실행 플로우
   - 단계별 처리 방법이나 실행 절차
   - 예: "고객 문의 시 먼저 이메일을 확인하고, 그 다음 전화로 연락한다"
   - **저장소: Skills**에 저장됨 (Claude Agent Skill로 업로드됨)
   - 이 시스템은 Claude Agent Skill 스펙(예: `https://code.claude.com/docs/ko/skills`)에 맞춰 자동으로 `SKILL.md`와 부가 파일을 생성합니다.
   - 당신은 아래 JSON의 `skill` 오브젝트를 통해 **스킬 마크다운 초안과 추가 코드/문서 파일 구조를 설계하는 역할**을 수행해야 합니다.
   - **SKILL 설계 시 필수/권장 사항:**
     * name (선택적이지만 강력 권장): 스킬 디렉터리/이름으로 쓰일 짧은 식별자
       - 소문자, 하이픈 기반(`my-skill-name`) 또는 짧은 영문 이름을 권장
       - 어떤 작업을 하는 스킬인지 한눈에 알 수 있게 작성
       - 피드백과 기존 스킬의 관계를 다음과 같이 구분해서 이름을 선택하세요:
         · 피드백이 기존 스킬을 **확장(EXTENDS)** 하거나 **정제/개선(REFINES)** 하거나 **완전히 대체(SUPERSEDES/CONFLICTS)** 하는 경우 →
           해당 기존 스킬의 이름을 그대로 사용하세요 (예: `global-investment-analyer`). 즉, 같은 스킬을 UPDATE하는 것입니다.
         · 피드백이 기존 어떤 스킬과도 본질적으로 무관(UNRELATED)하고, **새로운 작업 절차**를 정의한다면 →
           기존 것과 구분되는 **새 이름**을 생성하세요.
     * description (필수): SKILL.md frontmatter에 들어가는 설명
       - "무엇을 하는 스킬인지" + "언제/어떤 요청에 사용해야 하는지"를 1~3문장으로 명확히 기술
       - 사용자가 말할 법한 **트리거 키워드**를 포함 (예: "PDF", "요약", "에이전트 피드백", "SQL 분석")
     * overview (필수): 본문 상단 "개요" 섹션에 들어갈 설명
       - 스킬의 목적, 전반적인 흐름, 전형적인 사용 시나리오를 3~5문장 정도로 구체적으로 설명
       - 나중에 코드를 보지 않아도 이 설명만으로 스킬의 역할을 이해할 수 있어야 합니다.
       - **⚠️ 중요: 기존 스킬을 UPDATE하는 경우**, 기존 overview의 상세한 설명을 그대로 유지하거나, 
         피드백 요구사항을 반영하여 개선하되, 기존 내용의 풍부함과 구체성을 유지하세요.
         단순히 description을 복사한 수준으로 축소하지 마세요.
     * steps (필수): 단계별 실행 절차
       - 각 단계는 **명령형 문장**으로 작성하고, 입력/출력/주의사항을 최대한 구체적으로 서술합니다.
       - "1. 피드백 원문을 읽고 핵심 요약 3가지를 bullet로 정리한다"처럼 바로 실행 가능한 수준으로 작성합니다.
       - **⚠️ 중요: 기존 스킬을 UPDATE하는 경우 (EXTENDS/REFINES/SUPERSEDES 관계)**:
         · 기존 스킬의 상세한 워크플로우, 예시, 참고 문서, 사용 시점, 실전 예시 등 **모든 섹션과 내용을 보존**해야 합니다.
         · 피드백은 기존 내용에 **추가/개선**하는 형태로 통합하세요. 기존 내용을 삭제하거나 단순화하지 마세요.
         · 예: 기존 스킬에 "워크플로우", "자산별 투자 전략", "실전 사용 예시", "주요 데이터 소스" 등 상세 섹션이 있다면, 
           피드백 요구사항(예: "결과를 정확한 수치로 적힌 파일 생성")을 해당 섹션에 자연스럽게 추가하거나 관련 단계를 개선하세요.
         · 단순히 5-6줄의 steps만 나열하는 것이 아니라, 기존의 풍부한 구조와 내용을 유지하면서 피드백을 반영하세요.
     * usage (선택): 스킬 사용법과 주의사항
       - 언제 이 스킬을 호출해야 하는지, 어떤 입력이 필요하고 어떤 출력이 기대되는지, 피해야 할 오용 패턴 등을 기술합니다.
       - **⚠️ 중요: 기존 스킬을 UPDATE하는 경우**, 기존의 "사용 시점", "워크플로우", "실전 사용 예시" 등 
         상세한 사용법 섹션을 모두 보존하세요. 피드백이 새로운 사용 패턴을 추가한다면 기존 예시에 추가하되, 
         기존 예시들을 삭제하지 마세요.
     * additional_files (선택): 실제 실행 로직/예제/레퍼런스 등 부가 파일 구조 설계
       - **파일 분류 규칙**:
         · Python 코드 파일 → `scripts/` 폴더 (예: `"scripts/skill_logic.py"`, `"scripts/data_processor.py"`)
         · Markdown 문서 파일 → `references/` 폴더 (예: `"references/SCHEMA.md"`, `"references/API_GUIDE.md"`)
         · 예제/사용 가이드 문서 → `docs/` 폴더 (예: `"docs/EXAMPLES.md"`, `"docs/USAGE.md"`)
       - **파일 내용 작성 규칙**:
         · 각 value에는 **실제 파일 전체 내용**(코드 또는 Markdown 텍스트)을 포함해야 하며,
         · 이 내용만으로도 파일이 바로 저장/실행 가능하도록 자급자족(self-contained)하게 작성합니다.
       - **⚠️ 중요: 피드백이 새 기능/절차를 요구하는 경우 (필수!)**:
         · 피드백 요구사항에 따라 **반드시 필요한 파일들을 생성**하세요:
           - Python 코드가 필요한 경우 → `scripts/` 디렉토리에 `.py` 파일 생성 (전체 실행 가능한 코드)
           - 해당 코드에 대한 설명/가이드가 필요한 경우 → `references/` 디렉토리에 `.md` 파일 생성 (전체 문서 내용)
         · **예시: "경제 지표 조회 후 결과를 정확한 수치로 적힌 파일 생성" 피드백이면:**
           - `scripts/save_economic_data.py`: 파일 생성 로직 구현 (전체 Python 코드, import문부터 함수/클래스까지 완전한 코드)
           - `references/ECONOMIC_DATA_FORMAT.md`: 파일 형식, 사용법, 주의사항 설명 (전체 Markdown 내용, 헤더부터 예시까지 완전한 문서)
         · **모든 새 파일의 전체 내용을 `additional_files`에 포함하세요.**
         · 피드백이 코드 실행을 요구하면, 반드시 해당 Python 스크립트를 생성하고, 필요시 설명 문서도 함께 생성하세요.
       - **⚠️ 중요: 기존 스킬을 UPDATE하는 경우**:
         · 기존 스킬에 이미 파일들이 있다면, 피드백 요구사항에 따라:
           - 기존 파일을 수정해야 하는 경우: 해당 파일 경로와 수정된 전체 내용을 포함하세요.
           - **새 파일을 추가해야 하는 경우: 새 파일 경로와 내용을 추가하세요.**
         · 기존 파일들을 삭제하지 말고, 필요시 수정하거나 새 파일을 추가하세요.
         · 예: 피드백이 "경제 지표를 파일로 저장하는 기능 추가"를 요구한다면,
           - 기존 `scripts/collect_economic_indicators.py`가 있다면, 파일 저장 로직을 추가한 수정된 전체 코드를 포함하세요.
           - 또는 새 파일 `scripts/save_economic_data.py`를 추가하고, `references/ECONOMIC_DATA_FORMAT.md`도 함께 생성하세요.
         · 피드백 요구사항에 따라 Python 스크립트가 필요하면 `scripts/`에, 참고 문서가 필요하면 `references/`에 추가하세요.
   
4. **MIXED (혼합)**
   - 하나의 피드백에 여러 유형이 섞인 경우
   - 예: "주문 금액이 100만원 이상이면(의사결정 규칙) 먼저 확인하고(실행 규칙) 사용자에게 알려야 한다(기억)"
   - 각 유형별로 해당 저장소에 저장됨

**우선순위 규칙:**
- DMN_RULE (의사결정 규칙) > SKILL (실행 규칙) > MEMORY (기억) 순으로 우선순위가 높다
- 조건-결과 형태가 있으면 DMN_RULE (DMN Rule 저장소)이 우선
- 단계별 절차가 명확하면 SKILL (Skills 저장소)
- 그 외는 MEMORY (mem0 저장소)

**응답 형식:**
- 추가 설명 없이 오직 아래 JSON 구조로만 응답하세요
- 마크다운 코드블록(```)이나 기타 텍스트는 포함하지 마세요
- JSON 객체만 출력하세요

{{
  "target": "MEMORY | DMN_RULE | SKILL | MIXED",
  "artifacts": {{
    "memory": "MEMORY 타입일 경우의 내용 (mem0 저장소에 저장, 선택적)",
    "dmn": {{"name": "규칙 이름", "condition": "조건", "action": "결과"}} (DMN_RULE일 경우, DMN Rule 저장소에 저장, 선택적),
    "skill": {{
      "name": "스킬 이름 (선택적이지만 권장, 디렉터리/Skill 식별자로 사용)",
      "description": "스킬에 대한 간단한 설명 (frontmatter용, 필수. 무엇을/언제 할지와 트리거 키워드를 포함)",
      "overview": "스킬의 개요 및 목적에 대한 상세 설명 (본문에 표시될 개요 섹션, 필수)",
      "usage": "스킬 사용법 및 주의사항 (선택적, 필요시에만 포함)",
      "steps": ["1단계 설명", "2단계 설명", "..."],
      "additional_files": {{
        "scripts/skill_logic.py": "# 이 스킬이 실제로 수행할 Python 로직을 구현한 코드 (필요 시)",
        "docs/EXAMPLES.md": "# 이 스킬의 사용 예시/패턴/주의사항을 담은 Markdown 문서 (필요 시)",
        "references/SCHEMA.md": "# 관련 스키마나 외부 포맷을 정리한 참고 문서 (필요 시)"
      }} (선택적. 코드/문서가 필요한 경우에만 포함하며, 각 값에는 실제 파일 전체 내용을 넣습니다.)
      
      **⚠️ 기존 스킬 UPDATE 시 필수 지침:**
      - steps 배열에는 기존 스킬의 상세한 워크플로우 구조를 반영하세요.
        예: 기존에 "1. 거시경제 환경 분석", "2. 뉴스 모니터링", "3. 기술적 분석" 등 여러 단계가 있다면,
        피드백 요구사항(예: "결과를 파일로 생성")을 적절한 단계에 추가하거나 새로운 단계로 추가하되,
        기존 단계들을 모두 보존하세요.
      - overview에는 기존의 상세한 설명(목적, 제공 기능, 사용 시점 등)을 유지하되,
        피드백 요구사항을 자연스럽게 통합하세요.
      - additional_files에 기존 스킬의 scripts/, docs/, references/ 파일들이 있다면:
        · 기존 파일을 수정해야 하는 경우: 해당 파일 경로와 수정된 전체 내용을 포함하세요.
        · 새 파일을 추가해야 하는 경우: 새 파일 경로와 내용을 추가하세요.
        · Python 코드 파일은 `scripts/` 폴더에, Markdown 문서는 `references/` 폴더에 배치하세요.
        · 기존 파일들을 삭제하지 말고, 필요시 수정하거나 새 파일을 추가하세요.
        · 예: 피드백이 "경제 지표 결과를 정확한 수치로 파일 생성"을 요구한다면,
          - 기존 `scripts/collect_economic_indicators.py`가 있다면, 파일 저장 기능을 추가한 수정된 전체 코드를 포함하세요.
          - 또는 새 파일 `scripts/save_economic_data.py`를 추가할 수도 있습니다.
          - 참고 문서가 필요하면 `references/ECONOMIC_DATA_FORMAT.md` 같은 새 파일을 추가하세요.
    }} (SKILL일 경우, Skills 저장소에 저장, 선택적)
  }},
  "reasoning": "분류 이유를 간단히 설명"
}}
"""

    try:
        response = await llm.ainvoke(prompt)
        cleaned_content = clean_json_response(response.content)
        
        log(f"🔀 라우팅 LLM 응답: {cleaned_content}")
        
        parsed_result = json.loads(cleaned_content)
        
        target = parsed_result.get("target", "MEMORY")
        reasoning = parsed_result.get("reasoning", "")
        
        log(f"📊 학습 라우팅 결과: {target} (이유: {reasoning})")
        
        return {
            "target": target,
            "artifacts": parsed_result.get("artifacts", {}),
            "reasoning": reasoning
        }
        
    except json.JSONDecodeError as e:
        log(f"❌ 라우팅 JSON 파싱 실패 - 응답: {response.content if 'response' in locals() else 'None'}")
        handle_error("학습라우팅 JSON 파싱", f"응답 파싱 실패: {e}")
        # 기본값으로 MEMORY 반환
        return {
            "target": "MEMORY",
            "artifacts": {"memory": content},
            "reasoning": "JSON 파싱 실패로 기본값 사용"
        }
    except Exception as e:
        handle_error("학습라우팅", e)
        # 기본값으로 MEMORY 반환
        return {
            "target": "MEMORY",
            "artifacts": {"memory": content},
            "reasoning": f"에러 발생: {str(e)}"
        }
