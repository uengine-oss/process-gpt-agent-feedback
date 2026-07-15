-- openspec change: add-process-definition-apply (naming follow-up)
--
-- skill_feedback_proposals was named when this table only carried SKILL-target
-- feedback. It now carries SKILL/DMN_RULE/PROCESS_DEFINITION targets (see
-- feedback_proposal_targets.sql), so the name no longer matches what it stores.
-- This migration renames the table (and its constraint/index/trigger names) to
-- feedback_proposals, and re-creates the two RPC functions whose bodies
-- reference the table by name.
--
-- ONLY needed against an instance that already ran skill_feedback_proposals.sql
-- / feedback_proposal_targets.sql under the old name (e.g. the shared Supabase
-- project this repo deploys against). Those two files were updated in place to
-- create/reference "feedback_proposals" directly, so a brand-new setup running
-- them fresh already gets the new name and does not need this file at all.
--
-- This repo does not own the Supabase schema (no migration tooling here);
-- apply this against the project's Supabase instance directly, in the Supabase
-- SQL Editor.
--
-- IMPORTANT: this must be applied before (or atomically with) deploying the
-- application code change that follows it -- core/database.py after this
-- change queries "feedback_proposals", not "skill_feedback_proposals".

ALTER TABLE public.skill_feedback_proposals RENAME TO feedback_proposals;

ALTER TABLE public.feedback_proposals
    RENAME CONSTRAINT skill_feedback_proposals_pkey TO feedback_proposals_pkey;

ALTER TABLE public.feedback_proposals
    RENAME CONSTRAINT skill_feedback_proposals_status_check TO feedback_proposals_status_check;

ALTER INDEX public.skill_feedback_proposals_collecting_key
    RENAME TO feedback_proposals_collecting_key;

ALTER INDEX public.skill_feedback_proposals_status_idx
    RENAME TO feedback_proposals_status_idx;

ALTER TRIGGER skill_feedback_proposals_updated_at ON public.feedback_proposals
    RENAME TO feedback_proposals_updated_at;

-- Re-create: the table rename doesn't update the table names hardcoded inside
-- these function bodies (PL/pgSQL resolves them by name at call time, not by
-- OID), so both must be re-created against the new name. Logic is unchanged
-- from skill_feedback_proposals.sql / feedback_proposal_targets.sql -- only
-- "skill_feedback_proposals" -> "feedback_proposals" throughout.

CREATE OR REPLACE FUNCTION public.append_feedback_to_batch(
    p_tenant_id   text,
    p_proc_def_id text,
    p_activity_id text,
    p_todo_id     uuid,
    p_content     text,
    p_time        text,
    p_user_id     text
) RETURNS public.feedback_proposals
LANGUAGE plpgsql
AS $$
DECLARE
    v_item  jsonb;
    v_batch public.feedback_proposals;
BEGIN
    v_item := jsonb_build_object(
        'todo_id', p_todo_id,
        'content', p_content,
        'time', p_time,
        'user_id', p_user_id
    );

    INSERT INTO public.feedback_proposals (
        tenant_id, proc_def_id, activity_id, status, collected_items, first_collected_at
    ) VALUES (
        p_tenant_id, p_proc_def_id, p_activity_id, 'COLLECTING', jsonb_build_array(v_item), now()
    )
    ON CONFLICT (tenant_id, proc_def_id, activity_id) WHERE status = 'COLLECTING'
    DO UPDATE SET
        collected_items = public.feedback_proposals.collected_items || jsonb_build_array(v_item)
    RETURNING * INTO v_batch;

    RETURN v_batch;
END;
$$;

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
