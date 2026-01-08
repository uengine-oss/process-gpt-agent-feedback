# 배치 작업 시스템 설정 가이드

## 1. 데이터베이스 마이그레이션 (main.py 실행 전 필수)

### 실행 순서
다음 SQL 파일들을 **Supabase SQL Editor**에서 순서대로 실행하세요:

#### 1단계: 통합 변경 이력 테이블 생성
```sql
-- migrations/create_agent_knowledge_history.sql 실행
```
이 테이블은 MEMORY, DMN_RULE, SKILL의 모든 변경 이력을 통합 관리합니다.

#### 2단계: 배치 작업 이력 테이블 생성
```sql
-- migrations/create_batch_job_history.sql 실행
```
배치 작업의 실행 이력과 통계를 저장합니다.

#### 3단계: 배치 작업 백업 테이블 생성 (롤백용)
```sql
-- migrations/create_batch_job_backup.sql 실행
```
배치 작업으로 삭제/이동된 항목의 백업을 저장합니다 (롤백 기능용).

### 마이그레이션 체크리스트
- [ ] `agent_knowledge_history` 테이블 생성 완료
- [ ] `batch_job_history` 테이블 생성 완료
- [ ] `batch_job_backup` 테이블 생성 완료
- [ ] 인덱스 생성 확인
- [ ] 권한 설정 확인 (anon 역할에 SELECT, INSERT 권한)

### 기존 데이터 마이그레이션 (선택적)
기존 `skill_history` 테이블이 있고 데이터를 유지하려면:
```sql
-- create_agent_knowledge_history.sql 파일 하단의 마이그레이션 SQL 실행
INSERT INTO public.agent_knowledge_history (
    knowledge_type, knowledge_id, knowledge_name, agent_id, tenant_id, operation,
    previous_content, new_content, feedback_content, created_at
)
SELECT 
    'SKILL' as knowledge_type,
    skill_name as knowledge_id,
    skill_name as knowledge_name,
    agent_id,
    tenant_id,
    operation,
    previous_content::jsonb,
    new_content::jsonb,
    feedback_content,
    created_at
FROM public.skill_history;
```

## 2. 환경 변수 설정

### 필수 환경 변수
`.env` 파일에 다음 변수들이 설정되어 있어야 합니다:
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
```

### 배치 작업 환경 변수 (선택적)
```env
# 배치 작업 활성화 (기본값: true)
BATCH_DEDUP_ENABLED=true

# 배치 작업 실행 간격 (초 단위, 기본값: 3600 = 1시간)
BATCH_DEDUP_INTERVAL_SECONDS=3600

# DRY_RUN 모드 (기본값: true, 실제 실행 안 함)
BATCH_DEDUP_DRY_RUN=true
```

## 3. 배치 작업 실행 방법

### 방법 1: 주기적 자동 실행 (권장)
환경 변수 설정:
```env
BATCH_DEDUP_ENABLED=true
BATCH_DEDUP_INTERVAL_SECONDS=3600  # 1시간마다 실행
BATCH_DEDUP_DRY_RUN=false  # 실제 실행 (프로덕션)
```

`main.py` 실행 시 자동으로 백그라운드에서 주기적으로 실행됩니다.

### 방법 2: API 엔드포인트로 수동 실행

#### 모든 에이전트 처리 (DRY_RUN)
```bash
# 분석만 수행 (실제 삭제 안 함)
curl "http://localhost:8000/batch/deduplicate?dry_run=true"
```

#### 모든 에이전트 처리 (실제 실행)
```bash
# 실제 삭제/이동 수행
curl "http://localhost:8000/batch/deduplicate?dry_run=false"
```

#### 특정 에이전트만 처리
```bash
# 특정 에이전트 ID로 처리
curl "http://localhost:8000/batch/deduplicate?agent_id=YOUR_AGENT_ID&dry_run=false"
```

### 방법 3: Python 스크립트로 실행
```python
# test_batch_manual.py 실행
import asyncio
from core.batch_deduplicator import BatchDeduplicator

async def main():
    deduplicator = BatchDeduplicator()
    result = await deduplicator.execute_batch_deduplication(
        agent_id=None,  # None이면 모든 에이전트
        dry_run=True   # True면 분석만, False면 실제 실행
    )
    print(result)

asyncio.run(main())
```

## 4. 배치 작업 모니터링

### 배치 작업 이력 조회
```bash
# 최근 작업 목록 조회
curl "http://localhost:8000/batch/history?limit=10"

# 특정 작업 상세 조회
curl "http://localhost:8000/batch/history?job_id=batch_20250101_120000_abc123"
```

### 배치 작업 롤백
```bash
# 특정 배치 작업 롤백
curl -X POST "http://localhost:8000/batch/rollback/batch_20250101_120000_abc123"
```

## 5. 실행 순서 요약

### 초기 설정
1. ✅ 데이터베이스 마이그레이션 실행 (3개 SQL 파일)
2. ✅ 환경 변수 설정 (`.env` 파일)
3. ✅ `main.py` 실행

### 배치 작업 실행
1. **테스트 단계**: `BATCH_DEDUP_DRY_RUN=true`로 설정하고 API 호출
2. **검증 단계**: 결과 확인 후 문제 없으면 `BATCH_DEDUP_DRY_RUN=false`로 변경
3. **프로덕션**: `BATCH_DEDUP_ENABLED=true`로 자동 실행 활성화

## 6. 주의사항

### DRY_RUN 모드
- `BATCH_DEDUP_DRY_RUN=true`: 분석만 수행, 실제 삭제/이동 안 함
- `BATCH_DEDUP_DRY_RUN=false`: 실제 삭제/이동 수행
- **처음에는 반드시 DRY_RUN 모드로 테스트하세요!**

### 배치 작업 검증
- 배치 작업 실행 전 자동으로 검증 수행
- 검증 실패 시 작업 중단 (에러가 있는 경우)
- 경고는 로그에 기록하고 작업 계속 진행

### 롤백 기능
- 배치 작업 실행 시 자동으로 백업 생성
- 문제 발생 시 API로 롤백 가능
- **DRY_RUN 모드에서는 롤백 불가** (백업 생성 안 함)

## 7. 문제 해결

### 마이그레이션 오류
- 테이블이 이미 존재하는 경우: `CREATE TABLE IF NOT EXISTS`로 안전하게 처리
- 권한 오류: Supabase에서 anon 역할에 권한 부여 확인

### 배치 작업이 실행되지 않는 경우
- `BATCH_DEDUP_ENABLED=true` 확인
- 로그에서 에러 메시지 확인
- API 엔드포인트로 수동 실행 시도

### 롤백이 안 되는 경우
- `dry_run=true`로 실행한 작업은 롤백 불가
- `job_id` 확인
- 백업 데이터 존재 여부 확인

