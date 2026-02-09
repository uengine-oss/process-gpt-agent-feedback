# Process GPT Agent Feedback

에이전트 피드백을 분석하여 지식 저장소(MEMORY, DMN_RULE, SKILL)에 적절히 분류/저장하는 AI 기반 피드백 처리 시스템입니다.

## 📋 목차

- [주요 기능](#주요-기능)
- [아키텍처](#아키텍처)
- [시작하기](#시작하기)
- [환경 설정](#환경-설정)
- [사용 방법](#사용-방법)
- [API 엔드포인트](#api-엔드포인트)
- [프로젝트 구조](#프로젝트-구조)
- [테스트](#테스트)
- [배포](#배포)
- [참고 문서](#참고-문서)

## 🚀 주요 기능

### 1. ReAct 에이전트 기반 피드백 처리
- **Thought → Action → Observation** 패턴으로 피드백 분석
- LLM이 직접 추론하고 판단하여 CRUD 작업 수행
- 5단계 필수 추론 프레임워크로 안전한 지식 관리
- 단순 재시도 요청 자동 필터링

### 2. 에이전트 초기 지식 셋팅
- 에이전트의 **목표(goal)**와 **페르소나(persona)**를 분석하여 초기 지식 자동 생성
- 목표/페르소나에서 구체적인 규칙(DMN_RULE), 절차(SKILL), 선호도(MEMORY) 추출
- 기존 지식과의 관계 분석을 통한 스마트 병합

### 3. 지식 저장소 통합 관리
- **MEMORY**: 지침, 선호도, 맥락 정보 (mem0 벡터 저장소)
- **DMN_RULE**: 조건-결과 비즈니스 규칙 (Supabase `proc_def` 테이블)
- **SKILL**: 단계별 절차, 작업 순서 (HTTP API + MCP 서버)
  - Skill Creator를 통한 자동 SKILL.md 및 부가 파일 생성
  - 기존 스킬과의 관계 분석 및 스마트 병합

### 4. 하이브리드 병합 시스템
- 에이전트 중심 추론 + 도구의 안전한 병합 지원
- 관계 유형 분석 (DUPLICATE, EXTENDS, REFINES, EXCEPTION, CONFLICTS, SUPERSEDES, COMPLEMENTS, UNRELATED)
- 기존 지식 자동 보존 및 스마트 병합

### 5. 변경 이력 관리
- 모든 지식 변경 이력 통합 관리 (`agent_knowledge_history` 테이블)
- 변경 전후 상태 추적 및 감사(audit) 지원

## 🏗️ 아키텍처

### 핵심 설계 원칙

1. **ReAct 에이전트 기반 처리**: Chain 방식 폴백 제거, ReAct 전용
2. **에이전트 중심 추론**: 도구는 정보 제공, 최종 판단은 에이전트
3. **하이브리드 병합**: 에이전트가 관계 유형 판단 → 도구가 안전하게 병합
4. **Skill Creator 통합**: SKILL 생성 시 자동으로 SKILL.md 및 부가 파일 생성

### 지식 저장소

| 저장소 | 설명 | 저장 위치 |
|--------|------|-----------|
| **MEMORY** | 지침, 선호도, 맥락 정보 | mem0 (Supabase vector store) |
| **DMN_RULE** | 조건-결과 비즈니스 규칙 | Supabase `proc_def` 테이블 |
| **SKILL** | 단계별 절차, 작업 순서 | HTTP API + MCP 서버 (claude-skills) |

### 처리 워크플로우

1. **피드백 처리**: Supabase 폴링 → 피드백 매칭 → ReAct 에이전트 분석 → 지식 저장
2. **초기 지식 셋팅**: Goal/Persona 입력 → ReAct 에이전트 분석 → 지식 생성

자세한 아키텍처 설명은 [FEEDBACK_PROCESSING_ARCHITECTURE.md](./FEEDBACK_PROCESSING_ARCHITECTURE.md)를 참조하세요.

## 🚀 시작하기

### 사전 요구사항

- Python 3.12 이상
- [uv](https://github.com/astral-sh/uv) 패키지 관리자
- Supabase 계정 및 데이터베이스
- OpenAI API 키
- MCP 서버 (claude-skills, SKILL 저장용)

### 설치

1. **저장소 클론**
```bash
git clone <repository-url>
cd process-gpt-agent-feedback
```

2. **가상 환경 생성 및 의존성 설치**
```bash
# .env 파일에 환경변수 설정
uv venv
uv pip install -r requirements.txt
source .venv/Scripts/activate  # Windows
# 또는
source .venv/bin/activate      # Linux/Mac
```

3. **데이터베이스 마이그레이션**
   - Supabase SQL Editor에서 `function.sql` 실행  
   - `agent_feedback_task`, `get_memories` 등 피드백/메모리 관련 함수와  
     `agent_knowledge_history`, `agent_knowledge_registry` 테이블만 생성됩니다.

4. **서버 실행**
```bash
uv run main.py
# 또는
python main.py
```

서버는 기본적으로 `http://localhost:6789`에서 실행됩니다.

## ⚙️ 환경 설정

`.env` 파일에 다음 환경 변수를 설정하세요:

```env
# Supabase 설정
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key

# 데이터베이스 연결 (mem0용)
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=your_db_host
DB_PORT=5432
DB_NAME=your_db_name

# MCP 서버 설정 (claude-skills)
MCP_SERVER_URL=http://your-mcp-server:8765/mcp

# OpenAI API
OPENAI_API_KEY=your_openai_api_key

# 서버 포트 (선택적, 기본값: 6789)
PORT=6789

# 디버그 모드 (선택적)
DEBUG=false
```

## 📖 사용 방법

### 피드백 처리

시스템은 자동으로 Supabase의 피드백 테이블을 폴링하여 처리합니다 (기본 간격: 7초).

1. Supabase의 `agent_feedback_task` 테이블에 피드백 데이터 삽입
2. 시스템이 자동으로 폴링하여 처리
3. 처리 결과는 `agent_knowledge_history` 테이블에 기록

### 에이전트 초기 지식 셋팅

에이전트의 목표와 페르소나를 기반으로 초기 지식을 자동 생성합니다.

**API 호출 예시:**
```bash
curl -X POST "http://localhost:6789/setup-agent-knowledge" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-123",
    "goal": "월별 자재 소요 예측 정확도를 95% 이상으로 유지",
    "persona": "철저하고 꼼꼼한 성격으로, 데이터 기반 의사결정을 돕습니다"
  }'
```

**처리 과정:**
1. Goal/Persona 분석
2. 기존 지식 조회 및 관계 분석
3. DMN_RULE, SKILL, MEMORY 자동 생성/수정
4. 변경 이력 기록

## 🔌 API 엔드포인트

### POST `/setup-agent-knowledge`

에이전트 초기 지식 셋팅 API

**요청 본문:**
```json
{
  "agent_id": "string (필수)",
  "goal": "string (선택, 없으면 agent_info에서 가져옴)",
  "persona": "string (선택, 없으면 agent_info에서 가져옴)"
}
```

**응답:**
```json
{
  "output": "처리 결과 메시지",
  "intermediate_steps": [...],
  "agent_id": "agent-123",
  "used_tools": ["commit_to_memory", "commit_to_dmn_rule", "commit_to_skill"],
  "did_commit": true,
  "commit_successes": ["commit_to_memory", "commit_to_dmn_rule"]
}
```

### API 문서

서버 실행 후 `http://localhost:6789/docs`에서 Swagger UI를 통해 모든 API를 확인할 수 있습니다.

## 📁 프로젝트 구조

```
process-gpt-agent-feedback/
├── core/                          # 핵심 로직
│   ├── react_agent.py             # ReAct 에이전트 및 프롬프트
│   ├── react_tools.py             # 에이전트 도구 정의
│   ├── feedback_processor.py      # 피드백 처리 로직
│   ├── polling_manager.py         # 피드백 폴링 및 처리
│   ├── knowledge_retriever.py    # 지식 조회
│   ├── semantic_matcher.py        # 의미적 유사도 분석
│   ├── learning_router.py         # 학습 라우팅
│   ├── conflict_analyzer.py       # 충돌 분석
│   ├── skill_creator_committer.py # Skill Creator 통합
│   ├── skill_quick_validate.py    # Skill 검증
│   ├── database.py                # 데이터베이스 연결
│   ├── llm.py                     # LLM 유틸리티
│   ├── mcp_client.py              # MCP 클라이언트
│   ├── skill_api_client.py        # Skill API 클라이언트
│   └── learning_committers/       # 지식 저장소 커밋터
│       ├── memory_committer.py    # MEMORY CRUD
│       ├── dmn_committer.py        # DMN_RULE CRUD
│       └── skill_committer.py     # SKILL CRUD
├── tools/                          # 유틸리티 도구
│   └── knowledge_manager.py
├── utils/                          # 유틸리티
│   └── logger.py                   # 로깅 유틸리티
├── docs/                           # 문서
│   └── SKILL_CREATOR_WORKFLOW.md  # Skill Creator 워크플로우
├── tests/                          # 테스트
│   ├── test_feedback_flow.py
│   ├── test_learning_committers.py
│   ├── test_mcp_integration.py
│   ├── test_skill_format.py
│   └── ...
├── k8s/                            # Kubernetes 배포 설정
│   ├── deployment.yaml
│   ├── service.yaml
│   └── configmap.yaml.example
├── scripts/                        # 배포 스크립트
│   ├── deploy.ps1                 # Windows PowerShell
│   └── deploy.sh                   # Linux/macOS
├── main.py                         # FastAPI 애플리케이션 진입점
├── docker-compose.yml              # Docker Compose 설정
├── Dockerfile                      # Docker 이미지 빌드
├── requirements.txt                # Python 의존성
├── pyproject.toml                  # 프로젝트 메타데이터
├── function.sql                    # 데이터베이스 함수
├── FEEDBACK_PROCESSING_ARCHITECTURE.md  # 아키텍처 문서
└── README.md                       # 이 파일
```

## 🧪 테스트

```bash
# 가상 환경 활성화 후
pytest tests/

# 특정 테스트 실행
pytest tests/test_feedback_flow.py
pytest tests/test_learning_committers.py
pytest tests/test_mcp_integration.py
```

## 🐳 배포

### 이미지: `ghcr.io/uengine-oss/agent-feedback:latest`

### 스크립트로 빌드/푸시/배포

```powershell
# Windows PowerShell: 빌드만
.\scripts\deploy.ps1

# 빌드 + GHCR 푸시 (사전: docker login ghcr.io)
.\scripts\deploy.ps1 -Push

# 빌드 + k8s 배포
.\scripts\deploy.ps1 -Apply

# 빌드 + 푸시 + 배포
.\scripts\deploy.ps1 -Push -Apply
```

```bash
# Linux/macOS: 빌드만
./scripts/deploy.sh

# 빌드 + 푸시 + 배포
./scripts/deploy.sh --push --apply
```

### 수동 Docker 빌드/푸시

```bash
docker build -t ghcr.io/uengine-oss/agent-feedback:latest .
docker push ghcr.io/uengine-oss/agent-feedback:latest   # docker login ghcr.io 선행
```

### Kubernetes 배포

`k8s/deployment.yaml`은 이미 `ghcr.io/uengine-oss/agent-feedback:latest`를 사용합니다.

```bash
# ConfigMap/Secret 설정 후
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

자세한 배포 설정은 `k8s/` 디렉토리를 참조하세요.

## 📚 참고 문서

- [FEEDBACK_PROCESSING_ARCHITECTURE.md](./FEEDBACK_PROCESSING_ARCHITECTURE.md) - 상세 아키텍처 및 설계 원칙
- [docs/SKILL_CREATOR_WORKFLOW.md](./docs/SKILL_CREATOR_WORKFLOW.md) - Skill Creator 워크플로우

## 🔧 개발 환경

### 가상 환경 관리

```bash
# 가상 환경 생성
uv venv

# 가상 환경 활성화 (Windows)
source .venv/Scripts/activate

# 가상 환경 활성화 (Linux/Mac)
source .venv/bin/activate

# 가상 환경 비활성화
deactivate
```

### 의존성 관리

```bash
# 의존성 설치
uv pip install -r requirements.txt

# 새 패키지 추가
uv pip install <package-name>
uv pip freeze > requirements.txt
```

### 디버그 모드

```bash
# 환경 변수 설정
export DEBUG=true  # Linux/Mac
# 또는
set DEBUG=true     # Windows

# 서버 실행 (자동 리로드 활성화)
python main.py
```

## 📝 라이선스

[라이선스 정보 추가]

## 🤝 기여

[기여 가이드 추가]
