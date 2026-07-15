-- openspec change: add-feedback-batching (target classification follow-up)
--
-- Adds per-target classification/decision state to feedback_proposals.
-- Before this, a triggered batch produced exactly one artifact (extracted_rule,
-- SKILL-only) and the whole row carried one decision (APPROVED/REJECTED).
--
-- Now a triggered batch is classified into one or more targets (SKILL /
-- DMN_RULE / PROCESS_DEFINITION), each with its own artifact and its own
-- independent approve/reject decision. `extracted_rule` and
-- `candidate_skill_names` are left in place (additive-only, no migration
-- tooling in this repo) but are no longer written by new proposals except
-- `candidate_skill_names`, which stays SKILL-target-scoped.
--
-- Supabase SQL Editor에서 실행하세요 (skill_feedback_proposals.sql,
-- feedback_collected_count.sql과 동일하게 이 리포는 마이그레이션 툴을 소유하지 않음).
--
-- NOTE: this table was originally named skill_feedback_proposals; renamed to
-- feedback_proposals here (and via rename_feedback_proposals_table.sql for
-- already-deployed instances, openspec change: add-process-definition-apply)
-- since it no longer only carries SKILL-target feedback.

ALTER TABLE public.feedback_proposals
    ADD COLUMN IF NOT EXISTS targets jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 'RESOLVED' covers a proposal whose every target has reached a decision
-- (approved, rejected, or a mix) — replaces the single-target APPROVED/REJECTED
-- terminal states going forward. Both old and new values are kept in the
-- CHECK so already-decided rows from before this migration stay valid.
ALTER TABLE public.feedback_proposals
    DROP CONSTRAINT IF EXISTS feedback_proposals_status_check;
ALTER TABLE public.feedback_proposals
    ADD CONSTRAINT feedback_proposals_status_check
    CHECK (status IN ('COLLECTING', 'PROPOSED', 'APPROVED', 'REJECTED', 'DISCARDED', 'RESOLVED'));

-- Atomically decide a single target within a PROPOSED proposal's `targets`
-- array. Guards against deciding a target twice (only touches entries whose
-- status is still 'PENDING') and against acting on a proposal that isn't
-- PROPOSED. Once every target has left 'PENDING', the proposal's own status
-- flips COLLECTING/PROPOSED -> RESOLVED so it drops out of the pending list.
-- Returns the updated row, or NULL if there was nothing eligible to decide
-- (proposal not PROPOSED, target type not found, or target already decided).
CREATE OR REPLACE FUNCTION public.decide_feedback_proposal_target(
    p_batch_id          uuid,
    p_target_type       text,
    p_status            text,
    p_decided_by        uuid,
    p_decided_by_name   text,
    p_decided_by_email  text,
    p_decision_note     text
) RETURNS public.feedback_proposals
LANGUAGE plpgsql
AS $$
DECLARE
    v_row         public.feedback_proposals;
    v_targets     jsonb;
    v_elem        jsonb;
    v_found       boolean := false;
    v_all_decided boolean := true;
    i             int;
BEGIN
    SELECT * INTO v_row
    FROM public.feedback_proposals
    WHERE id = p_batch_id AND status = 'PROPOSED'
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_targets := v_row.targets;

    -- classify_and_extract_proposal은 같은 type(예: DMN_RULE)에 서로 다른 관심사가
    -- 여럿이면 그 type의 target을 여러 개 만들 수 있다 — type은 target을 유일하게
    -- 식별하지 않는다. 그래서 매칭되는 PENDING target 중 첫 번째 하나만 결정하고
    -- EXIT한다: 같은 type이 여럿이면 approve/reject를 여러 번 호출해 하나씩 순서대로
    -- 결정한다. 여기서 EXIT 없이 전부 다 결정해버리면(과거 버그) 호출 하나로 서로
    -- 다른 artifact 여러 개가 한꺼번에 APPROVED로 찍히지만, 그중 첫 번째만 실제로
    -- 적용되고 나머지는 결정 상태만 남긴 채 유실된다.
    FOR i IN 0 .. jsonb_array_length(v_targets) - 1 LOOP
        v_elem := v_targets -> i;
        IF (v_elem ->> 'type') = p_target_type AND (v_elem ->> 'status') = 'PENDING' THEN
            v_elem := v_elem || jsonb_build_object(
                'status', p_status,
                'decided_by', p_decided_by,
                'decided_by_name', p_decided_by_name,
                'decided_by_email', p_decided_by_email,
                'decision_note', p_decision_note,
                'decided_at', to_jsonb(now())
            );
            v_targets := jsonb_set(v_targets, ARRAY[i::text], v_elem);
            v_found := true;
            EXIT;
        END IF;
    END LOOP;

    IF NOT v_found THEN
        RETURN NULL;
    END IF;

    FOR i IN 0 .. jsonb_array_length(v_targets) - 1 LOOP
        IF (v_targets -> i ->> 'status') = 'PENDING' THEN
            v_all_decided := false;
        END IF;
    END LOOP;

    UPDATE public.feedback_proposals
    SET targets = v_targets,
        status = CASE WHEN v_all_decided THEN 'RESOLVED' ELSE status END
    WHERE id = p_batch_id
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$;
