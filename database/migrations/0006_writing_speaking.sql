-- Phase 3: Writing and Speaking baseline (FR-WRITE-*, FR-SPEAK-*).
--
-- Reuses the Phase 2 content model rather than a parallel one: a Writing
-- or Speaking test is an ordinary tests/test_versions/sections row (so the
-- catalog, practice/mock `kind`, draft/publish, immutability, admin
-- listing and import/export all apply unchanged), and an attempt at one is
-- an ordinary `attempts` row (same timing, ownership and terminal-state
-- trigger). What's new is only what objective questions can't express:
--
--   writing_prompts      Task 1/2 prompts hanging off a section
--   speaking_prompts     Part 1/2/3 scripted prompts hanging off a section
--   writing_responses    one essay per (attempt, prompt), autosaved
--   speaking_responses   one answer record per (attempt, prompt): audio
--                        metadata or a text-mode answer - never the audio
--                        itself (spec: recordings stay on the student's
--                        device by default; only metadata is stored)
--   self_reviews         a student's own rubric ratings, editable after
--                        submission (so it can't live on `attempts`, which
--                        is frozen once terminal)
--   attempts.assessment_status   'objective' (Phase 2) or 'not_assessed'
--                        (Writing/Speaking: FR-WRITE-07, FR-SPEAK-08 - no
--                        automated band score, ever, in the MVP)

-- ---------------------------------------------------------------------
-- 1. attempts: assessment provenance (FR-RESULT-04)
-- ---------------------------------------------------------------------

alter table public.attempts
  add column assessment_status text not null default 'objective'
    check (assessment_status in ('objective', 'not_assessed'));

comment on column public.attempts.assessment_status is 'objective: raw_score/estimated_band are meaningful. not_assessed: Writing/Speaking - no automated band score exists, raw_score/max_score/estimated_band stay null by design.';

-- ---------------------------------------------------------------------
-- 2. Prompts (content - same visibility/immutability rules as passages)
-- ---------------------------------------------------------------------

create table public.writing_prompts (
  id uuid primary key default gen_random_uuid(),
  section_id uuid not null references public.sections (id) on delete cascade,
  task_number integer not null check (task_number in (1, 2)),
  prompt text not null,
  instructions text,
  min_words integer not null default 150 check (min_words >= 0),
  recommended_minutes integer check (recommended_minutes > 0),
  -- Optional data table for Academic Task 1: {"columns": [...], "rows": [[...], ...]}.
  -- Rendered as an accessible HTML table; a chart image can be attached to
  -- the section as an ordinary media_assets row (type 'image') instead/as well.
  visual jsonb,
  letter_format boolean not null default false,
  -- Shown to the student only after submission, as guidance on what a
  -- strong answer covers - not a score.
  examiner_notes text,
  display_order integer not null,
  created_at timestamptz not null default now(),
  unique (section_id, display_order)
);

create table public.speaking_prompts (
  id uuid primary key default gen_random_uuid(),
  section_id uuid not null references public.sections (id) on delete cascade,
  part integer not null check (part in (1, 2, 3)),
  prompt text not null,
  -- Part 2 cue card bullet points.
  bullets jsonb,
  prep_seconds integer check (prep_seconds >= 0),
  speak_seconds integer check (speak_seconds > 0),
  display_order integer not null,
  created_at timestamptz not null default now(),
  unique (section_id, display_order)
);

-- ---------------------------------------------------------------------
-- 3. Responses (student data - same ownership rules as `responses`)
-- ---------------------------------------------------------------------

create table public.writing_responses (
  id uuid primary key default gen_random_uuid(),
  attempt_id uuid not null references public.attempts (id) on delete cascade,
  writing_prompt_id uuid not null references public.writing_prompts (id) on delete cascade,
  body text not null default '',
  word_count integer not null default 0,
  -- Objective diagnostics (FR-WRITE-04), computed by the server at submit.
  diagnostics jsonb,
  version integer not null default 1,
  saved_at timestamptz not null default now(),
  unique (attempt_id, writing_prompt_id)
);

create table public.speaking_responses (
  id uuid primary key default gen_random_uuid(),
  attempt_id uuid not null references public.attempts (id) on delete cascade,
  speaking_prompt_id uuid not null references public.speaking_prompts (id) on delete cascade,
  response_mode text not null check (response_mode in ('audio', 'text')),
  duration_seconds numeric check (duration_seconds >= 0),
  text_response text,
  completed boolean not null default false,
  version integer not null default 1,
  saved_at timestamptz not null default now(),
  unique (attempt_id, speaking_prompt_id)
);

comment on table public.speaking_responses is 'Metadata only (FR-SPEAK-06). The recording itself stays in the student''s browser and can be downloaded; it is never uploaded or retained.';

create table public.self_reviews (
  attempt_id uuid primary key references public.attempts (id) on delete cascade,
  -- {"criterion_key": 1-9} self ratings against the public rubric.
  ratings jsonb not null default '{}'::jsonb,
  -- Ticked checklist item keys.
  checklist jsonb not null default '[]'::jsonb,
  notes text,
  updated_at timestamptz not null default now()
);

comment on table public.self_reviews is 'Self-assessment (FR-RESULT-04 provenance: self_assessment). Never presented as a band score.';

-- ---------------------------------------------------------------------
-- 4. Answers are frozen once the attempt is terminal - enforced in the
--    database, not only the API (same instinct as Phase 2's triggers).
-- ---------------------------------------------------------------------

create or replace function public.prevent_terminal_attempt_answer_mutation()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  attempt_status text;
begin
  select status into attempt_status from public.attempts where id = coalesce(new.attempt_id, old.attempt_id);
  -- A cascade from deleting the attempt itself has no parent row left.
  if attempt_status is not null and attempt_status != 'in_progress' then
    raise exception 'attempt % is % and its answers can no longer change', coalesce(new.attempt_id, old.attempt_id), attempt_status;
  end if;
  return coalesce(new, old);
end;
$$;

create trigger writing_responses_frozen_once_terminal
  before insert or update or delete on public.writing_responses
  for each row execute function public.prevent_terminal_attempt_answer_mutation();

create trigger speaking_responses_frozen_once_terminal
  before insert or update or delete on public.speaking_responses
  for each row execute function public.prevent_terminal_attempt_answer_mutation();

-- Self-review is the opposite: only allowed once the attempt is finished.
create or replace function public.require_terminal_attempt_for_self_review()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  attempt_status text;
begin
  select status into attempt_status from public.attempts where id = new.attempt_id;
  if attempt_status = 'in_progress' then
    raise exception 'attempt % is still in progress; self-review opens after submission', new.attempt_id;
  end if;
  new.updated_at = now();
  return new;
end;
$$;

create trigger self_reviews_after_submission_only
  before insert or update on public.self_reviews
  for each row execute function public.require_terminal_attempt_for_self_review();

-- ---------------------------------------------------------------------
-- 5. Indexes
-- ---------------------------------------------------------------------

create index writing_prompts_section_idx on public.writing_prompts (section_id);
create index speaking_prompts_section_idx on public.speaking_prompts (section_id);
create index writing_responses_attempt_idx on public.writing_responses (attempt_id);
create index speaking_responses_attempt_idx on public.speaking_responses (attempt_id);
create index attempts_student_idx on public.attempts (student_id, created_at desc);

-- ---------------------------------------------------------------------
-- 6. RLS - prompts mirror passages; responses mirror `responses`.
-- ---------------------------------------------------------------------

alter table public.writing_prompts enable row level security;
alter table public.speaking_prompts enable row level security;
alter table public.writing_responses enable row level security;
alter table public.speaking_responses enable row level security;
alter table public.self_reviews enable row level security;

create policy "Content of the current published version is readable" on public.writing_prompts
  for select
  using (
    public.is_admin()
    or exists (
      select 1
      from public.sections s
      join public.tests t on t.current_version_id = s.test_version_id
      where s.id = writing_prompts.section_id
        and t.status = 'published'
    )
  );

create policy "Admins manage writing_prompts" on public.writing_prompts
  for all
  using (public.is_admin())
  with check (public.is_admin());

create policy "Content of the current published version is readable" on public.speaking_prompts
  for select
  using (
    public.is_admin()
    or exists (
      select 1
      from public.sections s
      join public.tests t on t.current_version_id = s.test_version_id
      where s.id = speaking_prompts.section_id
        and t.status = 'published'
    )
  );

create policy "Admins manage speaking_prompts" on public.speaking_prompts
  for all
  using (public.is_admin())
  with check (public.is_admin());

create policy "Writing responses via owned attempt" on public.writing_responses
  for all
  using (
    exists (
      select 1 from public.attempts a
      where a.id = writing_responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  )
  with check (
    exists (
      select 1 from public.attempts a
      where a.id = writing_responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );

create policy "Speaking responses via owned attempt" on public.speaking_responses
  for all
  using (
    exists (
      select 1 from public.attempts a
      where a.id = speaking_responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  )
  with check (
    exists (
      select 1 from public.attempts a
      where a.id = speaking_responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );

create policy "Self reviews via owned attempt" on public.self_reviews
  for all
  using (
    exists (
      select 1 from public.attempts a
      where a.id = self_reviews.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  )
  with check (
    exists (
      select 1 from public.attempts a
      where a.id = self_reviews.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );
