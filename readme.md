# Process GPT Agent Feedback

에이전트 피드백을 분석하여 스킬(SKILL)을 개선하는 AI 기반 피드백 처리 서비스입니다.

`deepagents` 라이브러리의 Deep Agent를 사용하며, 피드백을 기반으로 에이전트 스킬의 생성/수정/삭제/적재를 자동으로 수행합니다.

## 주요 기능

### 1. Deep Agent 기반 피드백 처리
- `deepagents.create_deep_agent`를 사용한 피드백 분석 및 스킬 개선
- 5단계 추론 프레임워크 (의도 분석 → 기존 스킬 파악 → 관계 분석 → 자기 검증 → 작업 실행)
- 단순 재시도 요청 자동 필터링

### 2. 스킬 관리
- **SKILL**: 단계별 절차, 작업 순서 (SKILL.md 파일 + 부가 파일)
- HTTP API를 통한 스킬 CRUD (생성/수정/삭제)
- 기존 스킬 재사용을 위한 에이전트 적재 기능
- `skills/` 디렉토리의 skill-creator 스킬을 참조하여 스킬 내용 생성

### 3. 피드백 배치 수집 → 분류 → 제안 승인
- 피드백을 즉시 처리하지 않고 `(tenant_id, proc_def_id, activity_id)` 기준 배치로 수집
- 처리 단위는 워크아이템이다: 완료(DONE)된 워크아이템의 피드백만 수집하고, 한 워크아이템에서 새로 생긴 피드백은 몇 건이든 한 번에 같은 배치에 들어간다
- 워크아이템 5개 또는 3일 중 먼저 오는 조건으로 배치 트리거 (피드백 건수가 아니라 워크아이템 수를 센다)
- 분류를 시작하면 배치를 `CLASSIFYING`으로 닫는다 — 그 뒤 같은 워크아이템에 남긴 피드백은 다음 배치(다음 회차)로 처리된다. 분류 호출이 실패한 배치는 폐기하지 않고 15분 뒤 다시 분류한다
- 칭찬·감사·단순 재실행 요청("잘했어요", "다시 해줘")은 분류 전에 걸러낸다 — 이것만 모인 배치는 분류 없이 폐기된다
- 트리거된 배치는 먼저 **무엇을 개선할 수 있는지 분류**된다 (LLM 한 콜, 한 배치가 여러 target에 동시에 해당할 수 있음):
  - **SKILL** — 절차/실행 규칙 → 일반화된 규칙 텍스트 (기존과 동일한 산출물)
  - **DMN_RULE** — 조건-결과 비즈니스 규칙 → `proc_def.definition`과 같은 형태의 decision/rules 패치
  - **PROCESS_DEFINITION** — 업무 흐름/구조 변경 → activities/sequences/gateways 패치
- 공통 관심사가 전혀 없으면 배치 폐기, 있으면 target별 아티팩트를 담은 `PROPOSED` 제안 생성
- 각 target은 **독립적으로 승인/거절**된다.
  - `SKILL` target 승인 → 피드백 매칭(LLM) → Deep Agent 분석 → 스킬 CRUD(HTTP API). 담당 에이전트가 없는 배치도 지원한다 — 이 경우 스킬은 사용자가 아니라 활동(`proc_def.definition.activities[].skills`)에 귀속된다.
  - `DMN_RULE` target 승인 → 라이브 `proc_def.definition`은 건드리지 않고, draft `proc_def_version` 행(JSON + DMN 1.3 XML)과 `resource_pull_requests` 병합 요청을 생성한다. 실제 라이브 반영(머지)은 별도 단계로 스코프 밖.
  - `PROCESS_DEFINITION` target 승인 → `DMN_RULE`과 동일한 패턴 — 라이브 `proc_def.definition`은 건드리지 않고, artifact의 activities/sequences/gateways를 라이브 정의 사본에 기계적으로 병합한 draft `proc_def_version` 행과 `resource_pull_requests` 병합 요청을 생성한다. LLM/스킬 호출 없이 승인 요청 안에서 동기적으로 처리되며, id가 매칭되지 않는 `MODIFY` 항목은 새 요소(`ADD`)로 강등된다.
- `GET /feedback-proposals`
- `GET /feedback-proposals/my-feedback?tenant_id=&user_id=&scope=participating|mine` — `participating`(기본)은 사용자가 참여 중인 인스턴스·담당 워크아이템·피드백을 남긴 워크아이템, `mine`은 피드백을 남긴 워크아이템만. 워크아이템마다 누가 남겼든 모든 피드백(작성자 이름·프로필 포함)을 처리 회차(배치)별로 묶어, 회차의 단계(완료 후 수집·수집 중·분류 중·폐기·승인 대기·반영 중·반영됨·병합됨 등), 분류된 target, 폐기 사유, 적용 결과(병합 요청·draft 버전)와 아직 수집되지 않은 피드백을 돌려준다. process-gpt-vue3 "내 피드백"(`/my-feedback`) 화면이 쓴다.
- `POST /feedback-proposals/{id}/targets/{type}/approve` (`type`: `SKILL` | `DMN_RULE` | `PROCESS_DEFINITION`) — 본문 `target_index`를 주면 그 위치의 target만 결정한다. `Authorization: Bearer <승인자 JWT>`를 실어야 SKILL 적용이 스킬 API를 호출할 수 있다(스킬 API가 요청자 JWT로 테넌트를 검증한다).
- `POST /feedback-proposals/{id}/targets/{type}/reject`
- 승인된 target마다 적용 상태와 결과를 target에 남긴다: `apply_status`(`APPLYING`/`APPLIED`/`NO_CHANGE`/`FAILED`), `apply_results[]`(`resource_type`, `resource_id`, `pull_request_id`, `draft_version`, `branch`), `apply_error`. 스킬 커밋이 실패하거나 대상 스킬이 없으면 `FAILED`로 드러난다(예전에는 조용히 넘어가 반영 완료로 찍혔다). 한 번의 스킬 개선은 SKILL.md와 부가 파일을 같은 브랜치·같은 병합 요청에 담는다.
- 폐기된 배치는 `discard_reason`을, 분류는 됐지만 개선할 기존 리소스가 없어 빠진 target은 `dropped_targets[]`(항목마다 `drop_reason`)를 남긴다.
- 자세한 내용은 `openspec/changes/add-feedback-batching/`(배치 수집·분류·제안 생성), `openspec/changes/add-feedback-proposal-apply/`(무에이전트 SKILL 적용, DMN_RULE draft+PR), `openspec/changes/add-process-definition-apply/`(PROCESS_DEFINITION draft+PR) 참고

## 아키텍처

### 처리 워크플로우

```
배치 수집(7초, 완료된 워크아이템 단위) → 배치 트리거 확인(900초, 워크아이템 5개/3일) → 분류+제안 생성(LLM, target별 아티팩트)
  → 제안(PROPOSED, target 1개 이상) → 사용자가 target별로 승인/반려
  → (SKILL 승인 시) 피드백 매칭(LLM) → Deep Agent 분석 → 스킬 CRUD(HTTP API) — 담당 에이전트 없으면 활동 귀속
  → (DMN_RULE 승인 시) draft proc_def_version(JSON+XML) + resource_pull_requests 병합 요청 생성 (라이브 proc_def는 그대로)
  → (PROCESS_DEFINITION 승인 시) draft proc_def_version(activities/sequences/gateways 병합, 라이브 BPMN XML 있으면 XML도 병합) + resource_pull_requests 병합 요청 생성 (라이브 proc_def는 그대로)
```
1. **배치 수집**: Supabase `agent_feedback_task`로 피드백이 있는 완료된 워크아이템을 조회해, 그 워크아이템의 새 피드백 전부를 `(tenant_id, proc_def_id, activity_id)` 기준 배치에 한 번에 적재 (7초 간격) — 즉시 처리하지 않음
2. **배치 트리거**: 워크아이템 5개 또는 3일 중 먼저 오는 조건을 확인해 배치를 `CLASSIFYING`으로 닫는다 (900초 간격)
3. **분류+제안 생성**: 트리거된 배치가 무엇을 개선할 수 있는지 LLM이 분류하고(`SKILL`/`DMN_RULE`/`PROCESS_DEFINITION`, 여러 개 동시 가능) target별 아티팩트를 생성 — 공통 관심사가 없으면 배치 폐기
4. **승인/반려**: 각 target을 사용자가 개별적으로 승인/거절 (`POST /feedback-proposals/{id}/targets/{type}/approve|reject`)
5. **`SKILL` target 승인 시** 피드백 매칭(LLM) → Deep Agent 분석 → 스킬 CRUD(HTTP API) 실행. 담당 에이전트가 없으면 활동 전용 경로로 실행되며, 스킬은 `proc_def.definition.activities[].skills`에 귀속된다.
6. **`DMN_RULE` target 승인 시** 라이브 `proc_def.definition`을 조회해 artifact를 병합한 draft `proc_def_version` 행(JSON + DMN 1.3 XML `snapshot`)을 만들고, `resource_pull_requests`에 병합 요청(`resource_type='dmn'`, `status='OPEN'`)을 연다 — git 저장소는 없고 이 테이블 안에서만 리뷰/이력이 관리된다. 이 draft를 라이브 `proc_def`에 실제로 반영(머지)하는 단계는 아직 없다.
7. **`PROCESS_DEFINITION` target 승인 시** 라이브 `proc_def.definition`을 조회해 artifact의 activities/sequences/gateways(`ADD`/`MODIFY`)를 사본에 병합한 draft `proc_def_version` 행(`version_tag='minor'`)을 만들고, `resource_pull_requests`에 병합 요청(`resource_type='bpmn'`, `status='OPEN'`)을 연다. `DMN_RULE`과 마찬가지로 LLM 호출이나 검증기 없이 id 매칭만으로 병합하며, id가 매칭되지 않는 `MODIFY`는 새 요소로 강등된다(지어낸 id는 재사용하지 않음). draft의 `snapshot`은 라이브 `proc_def.bpmn`에 실제 BPMN XML이 있으면 같은 변경을 그 XML에 반영해 채우고(`core.bpmn_xml.merge_process_definition_artifact_into_xml`), 라이브 XML이 없거나 병합에 실패하면 병합된 definition의 JSON 문자열로 폴백한다. 이 draft를 라이브 `proc_def`에 실제로 반영(머지)하는 단계도 아직 없다.

- 승인 전까지는 어떤 스킬도, 어떤 `proc_def`도 생성/수정/삭제되지 않는다.
- `DMN_RULE`/`PROCESS_DEFINITION` draft를 라이브 `proc_def`에 반영(머지)하는 것과, `resource_pull_requests`/`proc_def_version` draft를 실제로 소비하는 리뷰 화면이 있는지는 이 서비스 스코프 밖 — `openspec/changes/add-feedback-proposal-apply/design.md`, `openspec/changes/add-process-definition-apply/design.md`의 Open Questions 참고.

### Deep Agent 구성

| 구성 요소 | 설명 |
|-----------|------|
| **model** | LiteLLM Proxy 또는 OpenAI API를 통한 LLM |
| **tools** | `search_similar_skills`, `get_skill_detail`, `commit_to_skill`, `attach_skills_to_agent` (모두 HTTP API 전용, `SKILL_API_BASE_URL`) |
| **skills** | `SKILLS_DIR/anthropics-skills` 디렉토리 (skill-creator 등 전역 내장 스킬 참조, deepagents progressive disclosure 용도). `SKILLS_DIR`는 환경변수로 오버라이드 가능(기본값: 레포 안 `./skills/`) |
| **system_prompt** | 피드백 분석 및 스킬 개선 전문가 프롬프트 |

## 시작하기

### 사전 요구사항

- Python 3.12 이상
- [uv](https://github.com/astral-sh/uv) 패키지 관리자
- Supabase 계정 및 데이터베이스
- 스킬 HTTP API 서버 (process-gpt-deepagents가 제공, `core/api/skills_router.py` — 별도 claude-skills 서비스 아님)
- OpenAI API 키 또는 LiteLLM Proxy

### 설치

```bash
git clone <repository-url>
cd process-gpt-agent-feedback

uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 데이터베이스 마이그레이션

이 리포는 마이그레이션 툴을 소유하지 않으며, 셋업용 SQL 파일도 더 이상 함께 배포하지 않습니다(과거 일회성 스크립트는 이미 적용 후 제거됨). 새 인스턴스를 셋업하려면 아래 스키마를 Supabase에 직접 구성하세요:

- `agent_feedback_task` 함수 — 피드백 작업 조회
- `todolist.feedback_collected_count` 컬럼 — 같은 워크아이템에 피드백이 여러 번 추가되는 경우를 놓치지 않기 위함
- `feedback_proposals` 테이블(`targets` 컬럼 포함) — 배치 수집/제안 기능, target별 분류/독립 승인(`SKILL`/`DMN_RULE`/`PROCESS_DEFINITION`)에 필요
- `append_feedback_to_batch`, `decide_feedback_proposal_target` 함수
- `proc_def_version`, `resource_pull_requests` 테이블 — DMN_RULE/PROCESS_DEFINITION target 승인 시 draft 및 병합 요청 저장. `resource_pull_requests.requester_id`는 `uuid[]`(배열) 타입이어야 한다 — 병합 요청을 촉발한 피드백 작성자 전원을 담고, 승인자는 별도 `reviewer_id` 컬럼에 기록한다.
- `skill_contributions` 테이블 — 스킬별 사람 기여 이력(`GET /api/skills/{skill_name}/contributors`)에 필요
- `feedback_proposals.discard_reason`, `feedback_proposals.dropped_targets` 컬럼과 `patch_feedback_proposal_target`, `decide_feedback_proposal_target_at` 함수 — process-gpt-vue3 `supabase/migrations/20260922000001_feedback_processing_status.sql`
- `feedback_proposals.status`의 `CLASSIFYING`, 완료된 워크아이템만 고르는 `agent_feedback_task`, `append_workitem_feedback_to_batch` 함수 — process-gpt-vue3 `supabase/migrations/20260922000002_feedback_workitem_unit.sql`

### 환경 설정

`.env` 파일에 환경 변수를 설정하세요:

```env
# Supabase 설정 (필수)
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key

# LLM 모델 설정 (선택, 기본값: gpt-4o)
LLM_MODEL=gpt-4o
# LiteLLM Proxy (권장)
LLM_PROXY_URL=http://localhost:4000
LLM_PROXY_API_KEY=sk-...

# OpenAI API Key (선택, 프록시 없을 때 폴백)
OPENAI_API_KEY=your_openai_api_key

# 스킬 HTTP API 서버 (필수) — process-gpt-deepagents server.py 기본 포트
SKILL_API_BASE_URL=http://localhost:8888

# 서버 포트 (선택, 기본값: 6789)
PORT=6789

# 배치 트리거 확인 주기(초, 선택, 기본값: 900). 로컬 검증 때 줄여 쓴다.
FEEDBACK_BATCH_TRIGGER_INTERVAL=900

# 디버그 모드 (선택)
DEBUG=false
```

### 서버 실행

```bash
uv run main.py
# 또는
python main.py
```

서버는 `http://localhost:6789`에서 실행됩니다. 시작과 동시에 Supabase 피드백 테이블을 자동으로 폴링합니다.

## 프로젝트 구조

```
process-gpt-agent-feedback/
├── core/                              # 핵심 로직
│   ├── deep_agent.py                  # Deep Agent 생성 및 피드백 처리
│   ├── skill_tools.py                 # Deep Agent용 스킬 도구 (search, detail, commit, attach)
│   ├── feedback_processor.py          # 피드백 → 에이전트 매칭 (LLM) + 배치 분류/target별 제안 생성 (LLM)
│   ├── feedback_batch_manager.py      # 배치 수집/트리거 루프, 승인된 SKILL target 실행(apply_approved_proposal)
│   ├── feedback_proposal_routes.py    # 제안 목록/target별 승인·거절 API (FastAPI)
│   ├── polling_manager.py             # 에이전트 조회(get_agents_info) 및 스킬 개선 실행(process_feedback_task) — feedback_batch_manager와 수동 테스트 스크립트가 사용
│   ├── skill_api_client.py            # 스킬 HTTP API 클라이언트
│   ├── database.py                    # Supabase 데이터베이스 연결
│   ├── llm.py                         # LLM 생성 유틸리티
│   └── learning_committers/           # 스킬 저장소 커밋터
│       └── skill_committer.py         # 스킬 CRUD (HTTP API)
├── skills/                            # Deep Agent 참조 스킬
│   └── skill-creator/                 # 스킬 생성/수정 가이드 스킬
│       └── SKILL.md
├── utils/                             # 유틸리티
│   └── logger.py                      # 로깅
├── k8s/                               # Kubernetes 배포 설정
├── scripts/                           # 배포 스크립트
├── main.py                            # FastAPI 진입점
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── pyproject.toml
```

## 배포

### 이미지: `ghcr.io/uengine-oss/agent-feedback:latest`

### 스크립트로 빌드/푸시/배포

```bash
# Linux/macOS
./scripts/deploy.sh                # 빌드만
./scripts/deploy.sh --push --apply # 빌드 + 푸시 + 배포
```

```powershell
# Windows PowerShell
.\scripts\deploy.ps1               # 빌드만
.\scripts\deploy.ps1 -Push -Apply  # 빌드 + 푸시 + 배포
```

### Kubernetes 배포

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```
