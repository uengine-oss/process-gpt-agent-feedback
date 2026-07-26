-- openspec change: strategy-contribution-attribution
--
-- Records which humans contributed to each skill (created it, modified it, or
-- had a SKILL feedback proposal approved that produced/changed it), so
-- GET /api/skills/{skill_name}/contributors can compute a per-skill
-- contribution share. This repo does not own the Supabase schema (no
-- migration tooling here); apply this against the project's Supabase
-- instance directly.

CREATE TABLE IF NOT EXISTS public.skill_contributions (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            text NOT NULL,
    skill_name           text NOT NULL,
    contributor_user_id  text NOT NULL,
    contributor_name     text,
    contribution_type    text NOT NULL
                           CHECK (contribution_type IN ('CREATED', 'MODIFIED', 'PROPOSAL_APPROVED')),
    source_proposal_id   uuid,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS skill_contributions_skill_idx
    ON public.skill_contributions (tenant_id, skill_name);
