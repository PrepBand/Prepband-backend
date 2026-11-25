-- Phase 2, Step 3: exam vs. practice mode (FR-LIST-03/FR-TEST-10) and
-- audio transcripts shown only after submission (FR-LIST-06).
--
-- Applied 2026-11-19 to the live "PrepBand" Supabase project as migration
-- `step3_additions`. get_advisors(type: "security") came back clean
-- except the two already-known, intentional exceptions and the
-- pre-existing auth_leaked_password_protection flag.
--
-- Both are additive columns on tables that already exist and are already
-- governed by the existing RLS/immutability rules - no new tables, no new
-- policies needed. `transcript` deliberately never appears in the
-- pre-submission "safe content" schema (MediaAssetPublic in
-- app/schemas/attempts.py) - it only surfaces via
-- GET /attempts/{id}/results, which is already gated on the attempt being
-- terminal.

alter table public.attempts
  add column mode text not null default 'practice' check (mode in ('practice', 'exam'));

comment on column public.attempts.mode is 'practice: audio may be replayed/sought freely. exam: the frontend disables seeking - a UX restriction, not a grading rule, so this column is purely informational for the backend.';

alter table public.media_assets
  add column transcript text;

comment on column public.media_assets.transcript is 'Listening transcript. Never included in the pre-submission test-detail response - only surfaced via attempt results once terminal, same as answer_keys.';
