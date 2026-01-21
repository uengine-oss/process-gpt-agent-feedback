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

### 2. 지식 저장소 통합 관리
- **MEMORY**: 지침, 선호도, 맥락 정보 (mem0 벡터 저장소)
- **DMN_RULE**: 조건-결과 비즈니스 규칙 (Supabase `proc_def` 테이블)
- **SKILL**: 단계별 절차, 작업 순서 (HTTP API + MCP 서버)

### 3. 하이브리드 병합 시스템
- 에이전트 중심 추론 + 도구의 안전한 병합 지원
- `merge_mode` 파라미터로 의도 명시 (EXTEND, REFINE, REPLACE)
- 기존 지식 자동 보존 및 스마트 병합

### 4. 변경 이력 관리
- 모든 지식 변경 이력 통합 관리

## 🏗️ 아키텍처

### 핵심 설계 원칙

1. **ReAct 에이전트 기반 처리**: Chain 방식 폴백 제거, ReAct 전용
2. **에이전트 중심 추론**: 도구는 정보 제공, 최종 판단은 에이전트
3. **하이브리드 병합**: 에이전트가 `merge_mode` 선택 → 도구가 안전하게 병합

### 지식 저장소

| 저장소 | 설명 | 저장 위치 |
|--------|------|-----------|
| **MEMORY** | 지침, 선호도, 맥락 정보 | mem0 (Supabase vector store) |
| **DMN_RULE** | 조건-결과 비즈니스 규칙 | Supabase `proc_def` 테이블 |
| **SKILL** | 단계별 절차, 작업 순서 | HTTP API + MCP 서버 |

자세한 아키텍처 설명은 [FEEDBACK_PROCESSING_ARCHITECTURE.md](./FEEDBACK_PROCESSING_ARCHITECTURE.md)를 참조하세요.

## 🚀 시작하기

### 사전 요구사항

- Python 3.12 이상
- [uv](https://github.com/astral-sh/uv) 패키지 관리자
- Supabase 계정 및 데이터베이스
- OpenAI API 키
- MCP 서버 (SKILL 저장용)

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

# MCP 서버 설정
MCP_SERVER_URL=http://your-mcp-server:8765/mcp

# OpenAI API
OPENAI_API_KEY=your_openai_api_key

# 서버 포트 (선택적, 기본값: 6789)
PORT=6789
```

## 📖 사용 방법

### 피드백 처리

시스템은 자동으로 Supabase의 피드백 테이블을 폴링하여 처리합니다 (기본 간격: 7초).

## 🔌 API 엔드포인트

현재 기본 서버에서는 피드백 처리용 폴링/내부 로직만 사용합니다.

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
│   ├── knowledge_retriever.py     # 지식 조회
│   ├── semantic_matcher.py        # 의미적 유사도 분석
│   ├── learning_router.py         # 학습 라우팅
│   ├── database.py                # 데이터베이스 연결
│   ├── mcp_client.py              # MCP 클라이언트
│   ├── skill_api_client.py        # Skill API 클라이언트
│   └── learning_committers/        # 지식 저장소 커밋터
│       ├── memory_committer.py    # MEMORY CRUD
│       ├── dmn_committer.py       # DMN_RULE CRUD
│       └── skill_committer.py     # SKILL CRUD
├── tools/                          # 유틸리티 도구
│   └── knowledge_manager.py
├── utils/                          # 유틸리티
│   └── logger.py                   # 로깅 유틸리티
├── tests/                          # 테스트
│   ├── test_feedback_flow.py
│   ├── test_learning_committers.py
│   ├── test_mcp_integration.py
│   └── ...
├── k8s/                            # Kubernetes 배포 설정
│   ├── deployment.yaml
│   ├── service.yaml
│   └── configmap.yaml.example
├── main.py                         # FastAPI 애플리케이션 진입점
├── docker-compose.yml              # Docker Compose 설정
├── Dockerfile                      # Docker 이미지 빌드
├── requirements.txt                # Python 의존성
├── pyproject.toml                  # 프로젝트 메타데이터
├── function.sql                    # 데이터베이스 함수
├── FEEDBACK_PROCESSING_ARCHITECTURE.md  # 아키텍처 문서
└── readme.md                       # 이 파일
```

## 🧪 테스트

```bash
# 가상 환경 활성화 후
pytest tests/

# 특정 테스트 실행
pytest tests/test_feedback_flow.py
```

## 🐳 배포

### Docker 사용

```bash
# 이미지 빌드
docker build -t agent-feedback .

# 컨테이너 실행
docker-compose up -d
```

### Kubernetes 배포

```bash
# ConfigMap 및 Secret 설정
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secrets.yaml

# 배포
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

자세한 배포 설정은 `k8s/` 디렉토리의 파일들을 참조하세요.

## 📚 참고 문서

- [FEEDBACK_PROCESSING_ARCHITECTURE.md](./FEEDBACK_PROCESSING_ARCHITECTURE.md) - 상세 아키텍처 및 설계 원칙

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

## 📝 라이선스

[라이선스 정보 추가]

## 🤝 기여

[기여 가이드 추가]
