-- 2) DONE 상태이면서 피드백이 있는 작업 조회 및 상태 변경
CREATE OR REPLACE FUNCTION public.agent_feedback_task(
  p_limit    integer,
  p_consumer text
)
RETURNS SETOF todolist AS $$
BEGIN
  RETURN QUERY
    WITH cte AS (
      SELECT *
        FROM todolist
       WHERE draft_status = 'DONE' 
         AND feedback IS NOT NULL 
         AND feedback != '[]'::jsonb 
         AND feedback != '{}'::jsonb
         AND consumer IS NULL  -- 아직 처리되지 않은 작업만
       ORDER BY updated_at DESC
       LIMIT p_limit
       FOR UPDATE SKIP LOCKED
    ), upd AS (
      UPDATE todolist
         SET draft_status = 'FB_PROCESSING',  -- 피드백 처리 중 상태
             consumer     = p_consumer
        FROM cte
       WHERE todolist.id = cte.id
       RETURNING todolist.*
    )
    SELECT * FROM upd;
END;
$$ LANGUAGE plpgsql VOLATILE;


-- 익명(anon) 역할에 실행 권한 부여
GRANT EXECUTE ON FUNCTION public.agent_feedback_task(integer, text) TO anon;

