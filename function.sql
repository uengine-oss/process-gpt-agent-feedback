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
       WHERE feedback IS NOT NULL 
         AND feedback != '[]'::jsonb 
         AND feedback != '{}'::jsonb
         AND proc_def_id IS NOT NULL
         AND proc_def_id != ''
         AND (feedback_status IS NULL OR feedback_status = 'REQUESTED')
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
-- 4. 에이전트 지식 레지스트리 테이블
-- 에이전트가 가진 모든 지식(MEMORY, DMN_RULE, SKILL)의 메타데이터를 중앙에서 관리
-- 지식의 존재 여부를 빠르게 확인하고 조회할 수 있도록 지원
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_knowledge_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- 에이전트 정보
    agent_id UUID NOT NULL,
    tenant_id TEXT,
    
    -- 지식 정보
    knowledge_type TEXT NOT NULL CHECK (knowledge_type IN ('MEMORY', 'DMN_RULE', 'SKILL')),
    knowledge_id TEXT NOT NULL,
    knowledge_name TEXT,
    
    -- 지식 메타데이터
    content_summary TEXT,  -- 지식 내용 요약
    content_hash TEXT,  -- 지식 내용의 해시 (변경 감지용)
    
    -- 메타데이터
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ,  -- 마지막 접근 시간
    
    -- 제약 조건
    CONSTRAINT agent_knowledge_registry_agent_id_fkey FOREIGN KEY (agent_id, tenant_id) 
        REFERENCES public.users (id, tenant_id) ON UPDATE CASCADE ON DELETE CASCADE,
    
    -- 동일 에이전트의 동일 지식은 하나만 유지
    CONSTRAINT agent_knowledge_registry_unique UNIQUE (agent_id, knowledge_type, knowledge_id)
);

-- 인덱스 생성 (빠른 조회를 위해)
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_agent_id ON public.agent_knowledge_registry(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_tenant_id ON public.agent_knowledge_registry(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_knowledge_type ON public.agent_knowledge_registry(knowledge_type);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_knowledge_id ON public.agent_knowledge_registry(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_knowledge_name ON public.agent_knowledge_registry(knowledge_name);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_content_hash ON public.agent_knowledge_registry(content_hash);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_created_at ON public.agent_knowledge_registry(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_updated_at ON public.agent_knowledge_registry(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_last_accessed_at ON public.agent_knowledge_registry(last_accessed_at DESC);

-- 복합 인덱스 (가장 자주 사용되는 쿼리 패턴)
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_lookup ON public.agent_knowledge_registry(
    agent_id, knowledge_type
);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_registry_search ON public.agent_knowledge_registry(
    agent_id, knowledge_type, knowledge_name
);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_knowledge_registry TO anon;

-- updated_at 자동 업데이트 트리거 함수
CREATE OR REPLACE FUNCTION update_agent_knowledge_registry_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 트리거 생성
DROP TRIGGER IF EXISTS trigger_update_agent_knowledge_registry_updated_at ON public.agent_knowledge_registry;
CREATE TRIGGER trigger_update_agent_knowledge_registry_updated_at
    BEFORE UPDATE ON public.agent_knowledge_registry
    FOR EACH ROW
    EXECUTE FUNCTION update_agent_knowledge_registry_updated_at();





-- ============================================================================
-- 완료! 모든 테이블과 함수가 생성되었습니다.
-- ============================================================================
