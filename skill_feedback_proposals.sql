-- openspec change: skill-feedback-batching
--
-- NOTE: this table was originally named skill_feedback_proposals; renamed to
-- feedback_proposals here (and via rename_feedback_proposals_table.sql for
-- already-deployed instances, openspec change: add-process-definition-apply)
-- since it no longer only carries SKILL-target feedback.
--
-- This repo does not own the Supabase schema (no migration tooling here);
-- apply this against the project's Supabase instance directly.
--
-- Confirmed against a live local instance (2026-07-08):
--   - todolist.feedback_status is `text` (no enum) -- new status strings
--     ('COLLECTED', 'REJECTED') need no ALTER TYPE.
--   - agent_feedback_task(p_limit) only selects rows where
--     feedback_status IS NULL OR feedback_status = 'REQUESTED', so any other
--     value we set is already excluded from re-selection.
--   - public.update_updated_at_column() already exists and is reused here.

CREATE TABLE IF NOT EXISTS public.feedback_proposals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         text NOT NULL,
    proc_def_id       text NOT NULL,
    activity_id       text NOT NULL,
    status            text NOT NULL DEFAULT 'COLLECTING'
                        CHECK (status IN ('COLLECTING', 'PROPOSED', 'APPROVED', 'REJECTED', 'DISCARDED')),
    collected_items   jsonb NOT NULL DEFAULT '[]'::jsonb,
    first_collected_at timestamptz NOT NULL DEFAULT now(),
    extracted_rule    text,
    proposed_at       timestamptz,
    decided_by        uuid,
    decided_by_name   text,
    decided_by_email  text,
    decided_at        timestamptz,
    decision_note     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Only one COLLECTING batch may exist per (tenant, activity) at a time.
-- This backs the atomic upsert in append_feedback_to_batch() below.
CREATE UNIQUE INDEX IF NOT EXISTS feedback_proposals_collecting_key
    ON public.feedback_proposals (tenant_id, proc_def_id, activity_id)
    WHERE status = 'COLLECTING';

CREATE INDEX IF NOT EXISTS feedback_proposals_status_idx
    ON public.feedback_proposals (status);

CREATE TRIGGER feedback_proposals_updated_at
    BEFORE UPDATE ON public.feedback_proposals
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

-- Atomically append a feedback item to the current COLLECTING batch for
-- (tenant_id, proc_def_id, activity_id), creating the batch if none exists.
-- Concurrency-safe via the partial unique index above (INSERT ... ON CONFLICT).
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
-- openspec change: skill-feedback-batching
--
-- Adds a non-binding "candidate skill" hint to proposals, computed read-only
-- at PROPOSED time from the activity's configured skills + assigned agents'
-- existing skills. Purpose: let the frontend badge existing skill cards with
-- "new proposal" indicators without waiting for approval to know the target.
-- This is a hint only -- the Deep Agent decides the actual target(s) after
-- approval and may deviate from it (e.g. create a brand-new skill when the
-- candidate list is empty).

ALTER TABLE public.feedback_proposals
    ADD COLUMN IF NOT EXISTS candidate_skill_names text[] NOT NULL DEFAULT '{}';
