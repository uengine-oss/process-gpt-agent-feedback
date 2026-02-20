# 피드백 처리 아키텍처

에이전트 피드백을 분석하여 지식 저장소(MEMORY, DMN_RULE, SKILL)에 적절히 분류/저장하는 시스템.

## 핵심 설계 원칙

### 1. ReAct 에이전트 기반 처리
- **Thought → Action → Observation** 패턴으로 피드백 처리
- LLM이 직접 추론하고 판단하여 CRUD 작업 수행
- Chain 방식 폴백 제거, ReAct 전용

### 2. 에이전트 중심 추론 + 하이브리드 병합
- **도구는 정보 제공**, 최종 판단은 에이전트
- 유사도 점수에 의존하지 않고 **의미와 맥락** 분석
- 다층적 관계 분석 (DUPLICATE, EXTENDS, REFINES, CONFLICTS, EXCEPTION, SUPERSEDES, COMPLEMENTS, UNRELATED)
- **하이브리드 접근**: 에이전트가 `merge_mode`를 선택하면 도구가 안전하게 병합 처리

### 3. 도구의 안전한 병합 지원
- `merge_mode` 파라미터로 에이전트의 의도 명시
- **EXTEND**: 기존 내용 자동 보존 + 새 내용 추가 (도구가 병합)
- **REFINE**: 기존 내용 참조 후 일부 수정
- **REPLACE**: 완전 대체 (기존 동작)

---

## 지식 저장소

| 저장소 | 설명 | 저장 위치 |
|--------|------|-----------|
| **MEMORY** | 지침, 선호도, 맥락 정보 | mem0 (Supabase vector store) |
| **DMN_RULE** | 조건-결과 비즈니스 규칙 | Supabase `proc_def` 테이블 |
| **SKILL** | 단계별 절차, 작업 순서 | HTTP API + MCP 서버 |

---

## 핵심 파일 구조

```
core/
├── react_agent.py          # ReAct 에이전트 및 프롬프트
├── react_tools.py          # 에이전트 도구 정의
├── semantic_matcher.py     # 의미적 유사도 분석
├── polling_manager.py      # 피드백 폴링 및 처리
├── knowledge_retriever.py  # 지식 조회
└── learning_committers/
    ├── memory_committer.py # MEMORY CRUD
    ├── dmn_committer.py    # DMN_RULE CRUD
    └── skill_committer.py  # SKILL CRUD
```

---

## ReAct 에이전트 추론 프레임워크

에이전트는 반드시 다음 5단계 추론을 수행합니다:

### [STEP 1] 피드백 의도 분석
- 핵심 정보 파악
- 새로운 규칙인가? 기존 수정인가? 조건부 예외인가?
- 적용 조건/범위 확인

### [STEP 2] 기존 지식 심층 파악
- `search_similar_knowledge`로 유사 지식 검색
- `get_knowledge_detail`로 상세 내용 조회
- 범위 비교: 겹치는가? 포함되는가? 독립적인가?

### [STEP 3] 관계 추론 및 근거 제시
```
[관계 분석]
- 관계 유형: (EXTENDS / REFINES / DUPLICATE / ...)
- 판단 근거: (구체적 이유)
- 기존 지식 영향: (유지 / 수정 / 삭제)
```

### [STEP 4] 자기 검증
```
Q1: 내 판단이 틀렸다면, 다른 가능한 해석은?
Q2: 이 작업 후 기존 지식이 손상되는 부분이 있는가?
Q3: 최종 결과가 피드백 의도와 기존 지식 모두를 반영하는가?
```

### [STEP 5] 최종 상태 선언
```
[최종 상태 선언]
- 처리 전: (현재 상태)
- 처리 후: (예상 결과 - 구체적으로)
- 실행할 작업: (CREATE / UPDATE / DELETE / IGNORE)
```

---

## 관계 유형 → merge_mode 매핑 (핵심!)

| 유형 | 정의 | merge_mode | 기존 지식 처리 |
|------|------|------------|---------------|
| **DUPLICATE** | 표현만 다른 동일 내용 | (작업 안함) | 유지 |
| **EXTENDS** | 새 조건/케이스 추가 | `EXTEND` ✅ | 자동 보존 + 새 내용 추가 |
| **REFINES** | 기존 값/세부사항 변경 | `REFINE` | 해당 부분만 수정 |
| **EXCEPTION** | 기존 규칙의 예외 | `EXTEND` ✅ | 자동 보존 + 예외 규칙 추가 |
| **CONFLICTS** | 상충/모순 | 판단 필요 | 판단 필요 |
| **SUPERSEDES** | 명시적 대체 | `REPLACE` | 삭제 후 새로 생성 |
| **COMPLEMENTS** | 다른 측면 | `REPLACE` | 유지 + 별도 생성 (새 CREATE) |
| **UNRELATED** | 무관 | `REPLACE` | 유지 + 별도 생성 (새 CREATE) |

**✅ = 도구가 자동으로 기존 내용 조회 및 병합**

---

## 사용 가능한 도구

### 검색 도구
- `search_memory`: mem0에서 관련 메모리 검색
- `search_dmn_rules`: DMN 규칙 검색
- `search_skills`: 스킬 검색
- `search_similar_knowledge`: 통합 유사 지식 검색

### 분석 도구
- `get_knowledge_detail`: 특정 지식의 전체 상세 내용 조회
- `verify_knowledge_duplicate`: 중복 여부 확인
- `determine_operation`: 작업 결정 지원 (정보 제공)

### 저장 도구 (하이브리드 접근)
- `commit_to_memory`: MEMORY CRUD
  - `merge_mode`: REPLACE | EXTEND | REFINE
- `commit_to_dmn_rule`: DMN_RULE CRUD
  - `merge_mode`: REPLACE | EXTEND | REFINE
  - EXTEND 시 LLM으로 기존 XML + 새 규칙 자동 병합
- `attach_skills_to_agent`: 기존 스킬을 에이전트에 적재만 (스킬 생성/수정 없음)
  - 유사도가 높고 기존 스킬로 요구사항 충족 시 사용. 단일/멀티 스킬 지원.
- `commit_to_skill`: SKILL 생성/수정/삭제
  - `merge_mode`: REPLACE | EXTEND | REFINE
  - 기존 스킬로 충분하면 `attach_skills_to_agent` 우선, 새 절차 필요 시만 commit_to_skill

---

## SKILL 처리 의사결정

- **재사용 우선:** search_similar_knowledge에서 유사 스킬 발견 시, 기존 스킬이 요구사항을 충분히 커버하면 `attach_skills_to_agent` 사용 (새 스킬 생성 대신)
- **멀티 스킬:** 여러 스킬의 기능이 필요하면 `attach_skills_to_agent(skill_ids="skill-a, skill-b")`로 에이전트에 적재
- **생성/수정 필요 시:** 기존 스킬로 커버 불가일 때만 `commit_to_skill` (CREATE/UPDATE)

---

## 주요 수정 사항

### 2026-02-20: 스킬 재사용 및 외부 참조 제거

#### 1. attach_skills_to_agent 도구 추가
- 기존 스킬을 에이전트에 적재만 (스킬 생성/수정 없음)
- 유사도 높고 DUPLICATE/COMPLEMENTS 등으로 새 스킬 불필요 시 사용
- 멀티 스킬 지원 (skill_ids에 쉼표 구분)

#### 2. 스킬 마크다운 외부 참조 제거
- skill-creator는 `[관련 스킬 참고]`를 참고용 컨텍스트로만 사용
- 생성 결과 SKILL.md에는 외부 스킬 경로/링크를 포함하지 않음 (단일·독립 스킬 관리)

#### 3. search_similar_knowledge 재사용 가이드
- 유사도 0.85 이상 + DUPLICATE/COMPLEMENTS/EXTENDS 관계인 스킬 발견 시 attach_skills_to_agent 사용 권장 문구 포함

---

### 2026-01-08 (v2): 하이브리드 접근 도입

#### 1. merge_mode 파라미터 추가 (핵심 변경)
- 모든 commit 도구에 `merge_mode` 파라미터 추가
- **EXTEND**: 기존 내용 자동 보존 + 새 내용 추가 (도구가 병합)
- **REFINE**: 기존 내용 참조 후 일부 수정
- **REPLACE**: 완전 대체 (기존 동작)

#### 2. 도구 내 안전한 병합 구현
- `commit_to_dmn_rule`: EXTEND 시 `_extend_dmn_xml_llm` 호출하여 LLM으로 XML 병합
- `commit_to_memory`: EXTEND 시 기존 내용 + 새 내용 연결
- `commit_to_skill`: EXTEND 시 기존 steps + 새 steps 병합

#### 3. 프롬프트 업데이트
- 관계 유형 → merge_mode 매핑 테이블 추가
- 예시에 merge_mode 사용법 반영

---

### 2026-01-08 (v1): 초기 ReAct 구현

#### 1. 프롬프트 재설계
- 5단계 필수 추론 프레임워크 적용
- 자기 검증 단계 추가
- 최종 상태 선언 필수화
- 다양한 추론 예시 (EXTENDS, REFINES, SUPERSEDES) 포함

#### 2. 도구 입력 파싱 강화
- kwargs 형식 입력 자동 파싱 (`knowledge_type="DMN_RULE", knowledge_id="..."`)
- 중첩 JSON brace counting 파싱 (nested objects 처리)

#### 3. Committer 순수화
- `dmn_committer`: 자동 병합/확장 로직 제거
- `skill_committer`: 자동 CREATE→UPDATE 전환 로직 제거
- 에이전트가 최종 완성본을 직접 구성하여 전달

#### 4. MEMORY 조회 오류 수정
- 빈 쿼리로 semantic search 시 OpenAI API 오류 발생
- `get_knowledge_detail`에서 MEMORY 조회 시 직접 DB 조회로 변경

---

## 실행 방법

```bash
# 환경 설정
uv venv
uv pip install -r requirements.txt

# 서버 실행
uv run main.py
```

### 환경 변수 (.env)
```env
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=your_db_host
DB_PORT=5432
DB_NAME=your_db_name
MCP_SERVER_URL=http://your-mcp-server:8765/mcp
OPENAI_API_KEY=your_openai_api_key
```

---

## 주의사항

### merge_mode 선택이 핵심!
에이전트는 관계 유형을 파악한 후 적절한 `merge_mode`를 선택해야 합니다:

```
EXTENDS 관계 → merge_mode="EXTEND" (권장!)
REFINES 관계 → merge_mode="REFINE"
SUPERSEDES 관계 → merge_mode="REPLACE"
```

### EXTEND 모드 사용 시 (권장)
- **기존 ID 필수** (rule_id, memory_id, skill_id)
- **추가할 내용만 전달** (도구가 자동으로 기존 내용 조회 및 병합)
- 기존 지식이 **자동으로 보존**됨

### REPLACE 모드 사용 시
- 새 지식 생성: operation="CREATE" (ID 불필요)
- 기존 지식 대체: operation="UPDATE" + 기존 ID 필수
- **전달하는 내용이 최종 완성본**이어야 함

---

## 참고 문서

- `readme.md`: 기본 설정 안내

