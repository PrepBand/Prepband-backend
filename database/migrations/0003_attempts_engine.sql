-- Phase 2, Step 2: the shared test-taking engine (attempts, responses) and
-- its catalog/grading endpoints.
--
-- Applied 2026-11-19 to the live "PrepBand" Supabase project as migration
-- `attempts_engine`. get_advisors(type: "security") came back clean
-- except the two already-known, intentional exceptions (is_admin()/
-- is_email_registered() callable by anon/authenticated) and the
-- pre-existing auth_leaked_password_protection flag.
--
-- `attempts` points at a test_version, never at the mutable `tests` row -
-- this is exactly why Step 1's immutability work had to come first: once
-- a student starts an attempt, editing or republishing the test later
-- must never change what that attempt is graded against.

create table public.attempts (
  id uuid primary key default gen_random_uuid(),
  student_id uuid not null references auth.users (id) on delete cascade,
  test_version_id uuid not null references public.test_versions (id) on delete restrict,
  status text not null default 'in_progress' check (status in ('in_progress', 'submitted', 'expired', 'cancelled')),
  started_at timestamptz not null default now(),
  expires_at timestamptz,
  submitted_at timestamptz,
  raw_score numeric,
  max_score numeric,
  estimated_band numeric,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.attempts is 'Server-owned; expires_at is authoritative, the frontend countdown is only a display. Frozen once status leaves in_progress - see the trigger below.';

create table public.responses (
  id uuid primary key default gen_random_uuid(),
  attempt_id uuid not null references public.attempts (id) on delete cascade,
  question_id uuid not null references public.questions (id) on delete cascade,
  response jsonb not null,
  version integer not null default 1,
  saved_at timestamptz not null default now(),
  unique (attempt_id, question_id)
);

comment on table public.responses is 'One row per (attempt, question), upserted by PUT /attempts/{id}/responses/{questionId} - idempotent, never a new row per save.';

-- updated_at touch trigger, same pattern as tests_touch_updated_at.
create or replace function public.touch_attempts_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger attempts_touch_updated_at
  before update on public.attempts
  for each row
  execute function public.touch_attempts_updated_at();

-- Immutability: once an attempt leaves in_progress (submitted/expired/
-- cancelled), it's frozen - the same "a terminal state is final" rule as
-- test_versions, enforced the same way. The transition INTO a terminal
-- state is still allowed (old.status is still 'in_progress' at that
-- point); only a second write after that is blocked.
create or replace function public.prevent_terminal_attempt_mutation()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if old.status != 'in_progress' then
    raise exception 'attempt % is % and no longer editable', old.id, old.status;
  end if;
  return coalesce(new, old);
end;
$$;

create trigger attempts_immutable_once_terminal
  before update or delete on public.attempts
  for each row
  execute function public.prevent_terminal_attempt_mutation();

alter table public.attempts enable row level security;
alter table public.responses enable row level security;

create policy "Own attempts are readable" on public.attempts
  for select
  using (student_id = (select auth.uid()) or public.is_admin());

create policy "Own attempts are writable" on public.attempts
  for all
  using (student_id = (select auth.uid()) or public.is_admin())
  with check (student_id = (select auth.uid()) or public.is_admin());

create policy "Responses readable via owned attempt" on public.responses
  for select
  using (
    exists (
      select 1 from public.attempts a
      where a.id = responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );

create policy "Responses writable via owned attempt" on public.responses
  for all
  using (
    exists (
      select 1 from public.attempts a
      where a.id = responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  )
  with check (
    exists (
      select 1 from public.attempts a
      where a.id = responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );
