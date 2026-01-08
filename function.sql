-- ============================================================================
-- Process GPT Agent Feedback - Database Schema & Functions
-- 모든 테이블, 인덱스, 함수를 포함한 통합 SQL 파일
-- Supabase SQL Editor에서 순서대로 실행하세요
-- ============================================================================


-- ============================================================================
-- 1. 피드백 작업 조회 함수
-- ============================================================================

-- DONE 상태이면서 피드백이 있는 작업 조회 및 상태 변경
CREATE OR REPLACE FUNCTION public.agent_feedback_task(
  p_limit integer
)
RETURNS SETOF todolist AS $$
BEGIN
  RETURN QUERY
    WITH cte AS (
      SELECT *
        FROM todolist
       WHERE feedback_status IS NULL 
         AND feedback IS NOT NULL 
         AND feedback != '[]'::jsonb 
         AND feedback != '{}'::jsonb
         AND proc_def_id IS NOT NULL
         AND proc_def_id != ''
       ORDER BY updated_at DESC
       LIMIT p_limit
       FOR UPDATE SKIP LOCKED
    ), upd AS (
      UPDATE todolist
         SET feedback_status = 'STARTED'
        FROM cte
       WHERE todolist.id = cte.id
       RETURNING todolist.*
    )
    SELECT * FROM upd;
END;
$$ LANGUAGE plpgsql VOLATILE;

GRANT EXECUTE ON FUNCTION public.agent_feedback_task(integer) TO anon;


-- ============================================================================
-- 2. Mem0 Vector Store 함수 (vecs 스키마)
-- ※ vecs 스키마가 있는 경우에만 실행하세요
-- ============================================================================

-- 조회 (agent_id 필터링 + limit)
CREATE OR REPLACE FUNCTION public.get_memories(agent text, lim int default 100)
RETURNS SETOF vecs.memories
LANGUAGE sql STABLE
SECURITY DEFINER
AS $$
  SELECT *
  FROM vecs.memories
  WHERE (agent IS NULL OR metadata->>'agent_id' = agent)
  ORDER BY id
  LIMIT lim;
$$;

-- 단일 행 삭제
CREATE OR REPLACE FUNCTION public.delete_memory(mem_id text)
RETURNS void
LANGUAGE sql VOLATILE
SECURITY DEFINER
AS $$
  DELETE FROM vecs.memories WHERE id = mem_id;
$$;

-- 특정 agent_id의 모든 메모리 삭제
CREATE OR REPLACE FUNCTION public.delete_memories_by_agent(agent text)
RETURNS void
LANGUAGE sql VOLATILE
SECURITY DEFINER
AS $$
  DELETE FROM vecs.memories WHERE metadata->>'agent_id' = agent;
$$;

GRANT EXECUTE ON FUNCTION public.get_memories(text, int) TO anon;
GRANT EXECUTE ON FUNCTION public.delete_memory(text) TO anon;
GRANT EXECUTE ON FUNCTION public.delete_memories_by_agent(text) TO anon;


-- ============================================================================
-- 3. 에이전트 지식 변경 이력 테이블 (통합)
-- MEMORY, DMN_RULE, SKILL 모든 지식 타입의 변경 이력 관리
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_knowledge_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_type TEXT NOT NULL CHECK (knowledge_type IN ('MEMORY', 'DMN_RULE', 'SKILL')),
    knowledge_id TEXT NOT NULL,
    knowledge_name TEXT,
    agent_id UUID NOT NULL,
    tenant_id TEXT,
    operation TEXT NOT NULL CHECK (operation IN ('CREATE', 'UPDATE', 'DELETE', 'MOVE')),
    
    -- 변경 내용
    previous_content TEXT,
    new_content TEXT,
    
    -- 이동 정보 (MOVE인 경우)
    moved_from_storage TEXT CHECK (moved_from_storage IN ('MEMORY', 'DMN_RULE', 'SKILL')),
    moved_to_storage TEXT CHECK (moved_to_storage IN ('MEMORY', 'DMN_RULE', 'SKILL')),
    
    -- 메타데이터
    feedback_content TEXT,
    batch_job_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT agent_knowledge_history_agent_id_fkey FOREIGN KEY (agent_id, tenant_id) 
        REFERENCES public.users (id, tenant_id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_knowledge_history_knowledge_type ON public.agent_knowledge_history(knowledge_type);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_history_knowledge_id ON public.agent_knowledge_history(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_history_agent_id ON public.agent_knowledge_history(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_history_tenant_id ON public.agent_knowledge_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_history_operation ON public.agent_knowledge_history(operation);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_history_created_at ON public.agent_knowledge_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_history_batch_job_id ON public.agent_knowledge_history(batch_job_id);

GRANT SELECT, INSERT ON public.agent_knowledge_history TO anon;


-- ============================================================================
-- 4. 배치 작업 이력 테이블
-- 중복 제거 등 배치 작업의 실행 이력과 통계 저장
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.batch_job_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id TEXT NOT NULL UNIQUE,
    agent_id UUID,
    tenant_id TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED', 'ROLLED_BACK')),
    dry_run BOOLEAN NOT NULL DEFAULT false,
    
    -- 통계
    total_agents INTEGER DEFAULT 0,
    processed_agents INTEGER DEFAULT 0,
    total_deleted INTEGER DEFAULT 0,
    total_moved INTEGER DEFAULT 0,
    total_kept INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0,
    
    -- 결과 요약
    summary JSONB,
    
    -- 에러 정보
    error_message TEXT,
    error_details JSONB,
    
    CONSTRAINT batch_job_history_agent_id_fkey FOREIGN KEY (agent_id, tenant_id) 
        REFERENCES public.users (id, tenant_id) ON UPDATE CASCADE ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_job_history_job_id ON public.batch_job_history(job_id);
CREATE INDEX IF NOT EXISTS idx_batch_job_history_agent_id ON public.batch_job_history(agent_id);
CREATE INDEX IF NOT EXISTS idx_batch_job_history_tenant_id ON public.batch_job_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_batch_job_history_started_at ON public.batch_job_history(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_job_history_status ON public.batch_job_history(status);

GRANT SELECT, INSERT, UPDATE ON public.batch_job_history TO anon;


-- ============================================================================
-- 5. 배치 작업 백업 테이블 (롤백용)
-- 배치 작업으로 삭제/이동된 항목의 백업 저장
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.batch_job_backup (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id TEXT NOT NULL,
    agent_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    
    -- 백업 항목 정보
    storage_type TEXT NOT NULL CHECK (storage_type IN ('MEMORY', 'DMN_RULE', 'SKILL')),
    item_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('DELETE', 'MOVE')),
    
    -- 백업 내용
    original_content JSONB NOT NULL,
    
    -- 이동 정보 (MOVE인 경우)
    moved_to_storage TEXT,
    moved_to_id TEXT,
    
    -- 메타데이터
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT batch_job_backup_job_id_fkey FOREIGN KEY (job_id) 
        REFERENCES public.batch_job_history(job_id) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT batch_job_backup_agent_id_fkey FOREIGN KEY (agent_id, tenant_id) 
        REFERENCES public.users (id, tenant_id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_batch_job_backup_job_id ON public.batch_job_backup(job_id);
CREATE INDEX IF NOT EXISTS idx_batch_job_backup_agent_id ON public.batch_job_backup(agent_id);
CREATE INDEX IF NOT EXISTS idx_batch_job_backup_tenant_id ON public.batch_job_backup(tenant_id);
CREATE INDEX IF NOT EXISTS idx_batch_job_backup_storage_type ON public.batch_job_backup(storage_type);

GRANT SELECT, INSERT, DELETE ON public.batch_job_backup TO anon;


-- ============================================================================
-- 완료! 모든 테이블과 함수가 생성되었습니다.
-- ============================================================================
