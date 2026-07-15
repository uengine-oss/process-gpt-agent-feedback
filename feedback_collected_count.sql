-- ============================================================================
-- 워크아이템(todolist) 단위로 피드백이 여러 번 추가될 수 있는 문제 수정
--
-- 배경: agent_feedback_task는 feedback_status가 NULL/REQUESTED인 행만 재조회한다.
-- 우리 수집/처리 로직은 처리 후 feedback_status를 종결 상태(COLLECTED/PROCESSING 등
-- 거쳐 COMPLETED/FAILED)로 바꾸는데, 그 이후 같은 워크아이템의 feedback 배열에 새
-- 항목이 추가돼도 feedback_status를 되돌리는 코드가 어디에도 없다(이 리포도
-- deepagents 리포도 feedback 컬럼에 쓰지 않음 — 외부 시스템이 담당). 그 결과 같은
-- 워크아이템에 두 번째 피드백이 들어와도 영원히 재조회되지 않아 누락된다.
--
-- 해결: feedback_status와 무관하게 "배열 길이가 지금까지 수집한 개수보다 커졌으면"
-- 재조회 대상에 포함하도록 RPC를 바꾼다. 기존 조건은 OR로 유지하므로 기존 동작은
-- 그대로 보존된다(additive).
--
-- Supabase SQL Editor에서 실행하세요. (스킬 배치 마이그레이션과 마찬가지로 이 리포는
-- 마이그레이션 툴을 소유하지 않으므로 직접 적용)
-- ============================================================================

ALTER TABLE public.todolist
    ADD COLUMN IF NOT EXISTS feedback_collected_count integer NOT NULL DEFAULT 0;

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
         AND (
           (feedback_status IS NULL OR feedback_status = 'REQUESTED')
           OR (
             jsonb_typeof(feedback) = 'array'
             AND jsonb_array_length(feedback) > COALESCE(feedback_collected_count, 0)
           )
         )
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
