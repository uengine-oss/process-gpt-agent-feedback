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

### 3. 변경 이력 관리
- 모든 스킬 변경 이력 통합 관리 (`agent_knowledge_history` 테이블)
- 변경 전후 상태 추적

### 4. 피드백 배치 수집 및 승인 (`USE_BATCHED_FEEDBACK=true`)
- 피드백을 즉시 처리하지 않고 `(tenant_id, proc_def_id, activity_id)` 기준 배치로 수집
- 5건 또는 3일 중 먼저 오는 조건으로 배치 트리거 → LLM이 공통 규칙만 추출(스킬은 아직 건드리지 않음)
- 공통 규칙이 없으면 배치 폐기, 있으면 `PROPOSED` 제안 생성 — 사용자가 승인해야만 실제 스킬 개선 실행
- `GET /feedback-proposals`, `POST /feedback-proposals/{id}/approve`, `POST /feedback-proposals/{id}/reject`
- 자세한 내용은 `openspec/changes/add-feedback-batching/`(설계·스펙) 참고

## 아키텍처

### 처리 워크플로우

`USE_BATCHED_FEEDBACK` 플래그로 두 방식 중 하나만 동작한다(둘을 동시에 켜면 같은 큐를 경쟁적으로 소비하게 되므로 배타적으로 운영):

**기본값 (`USE_BATCHED_FEEDBACK` 미설정/false) — 즉시 처리:**
```
Supabase 폴링(7초) → 피드백 매칭(LLM) → Deep Agent 분석 → 스킬 CRUD(HTTP API)
```
1. **피드백 폴링**: Supabase `agent_feedback_task` 테이블에서 피드백 작업 조회 (7초 간격)
2. **피드백 매칭**: LLM이 피드백을 에이전트별로 분류하고 학습 후보 생성
3. **Deep Agent 처리**: `create_deep_agent`로 에이전트를 생성하여 스킬 개선 수행
4. **스킬 저장**: HTTP API를 통해 스킬 파일 업로드/수정/삭제

**`USE_BATCHED_FEEDBACK=true` — 배치 수집 + 승인:**
```
배치 수집(7초) → 배치 트리거 확인(900초, 5건/3일) → 규칙 추출(LLM) → 제안(PROPOSED)
  → 사용자 승인/반려 → (승인 시) 피드백 매칭(LLM) → Deep Agent 분석 → 스킬 CRUD(HTTP API)
```
- 승인 전까지는 어떤 스킬도 생성/수정/삭제되지 않는다.
- 담당 에이전트가 없는 배치(활동 전용 스킬)는 현재 미지원 — 승인 시 `FAILED`로 처리됨(알려진 제약, `openspec/changes/add-feedback-batching/design.md` 참고).

### Deep Agent 구성

| 구성 요소 | 설명 |
|-----------|------|
| **model** | LiteLLM Proxy 또는 OpenAI API를 통한 LLM |
| **tools** | `search_similar_skills`, `get_skill_detail`, `commit_to_skill`, `attach_skills_to_agent` |
| **skills** | `./skills/` 디렉토리 (skill-creator 스킬 참조) |
| **system_prompt** | 피드백 분석 및 스킬 개선 전문가 프롬프트 |

## 시작하기

### 사전 요구사항

- Python 3.12 이상
- [uv](https://github.com/astral-sh/uv) 패키지 관리자
- Supabase 계정 및 데이터베이스
- 스킬 HTTP API 서버 (claude-skills backend)
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

Supabase SQL Editor에서 `function.sql` 실행:
- `agent_feedback_task` 함수
- `agent_knowledge_history`, `agent_knowledge_registry` 테이블

배치 수집/제안 기능(`USE_BATCHED_FEEDBACK=true`)을 쓰려면 `skill_feedback_proposals.sql`도 함께 실행하세요:
- `skill_feedback_proposals` 테이블, `append_feedback_to_batch` 함수

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

# 스킬 HTTP API 서버 (필수)
SKILL_API_BASE_URL=http://localhost:8765

# 피드백 배치 수집+승인 플로우 사용 여부 (선택, 기본값: false → 기존 즉시 처리)
# true로 켜기 전 skill_feedback_proposals.sql 적용 필요. 레거시 즉시 처리 루프와 동시에 켜지 말 것.
USE_BATCHED_FEEDBACK=false

# 서버 포트 (선택, 기본값: 6789)
PORT=6789

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
│   ├── feedback_processor.py          # 피드백 → 에이전트 매칭 (LLM)
│   ├── polling_manager.py             # Supabase 피드백 폴링 및 처리
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
