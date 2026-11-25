-- Phase 4: full mock orchestration and unofficial completion certificates
-- (FR-MOCK-*, FR-CERT-*, spec §7 `mock_attempts` / `certificates`).
--
-- A full mock is a parent row that pins one published test_version per
-- module when it starts (FR-MOCK-01) and links the four ordinary
-- `attempts` rows as each module is started. Module attempts reuse the
-- Phase 2/3 engine unchanged (timer, autosave, submit, grading,
-- immutability) - FR-MOCK-03.
--
-- Every write to these two tables goes through the backend API, which
-- verifies ownership, order and eligibility. Students get read-only RLS
-- access and no write policy at all, so a certificate can never be
-- created or altered from the client (FR-CERT-07).

-- ---------------------------------------------------------------------
-- 1. mock_attempts
-- ---------------------------------------------------------------------

create table public.mock_attempts (
  id uuid primary key default gen_random_uuid(),
  student_id uuid not null references auth.users (id) on delete cascade,
  track text not null check (track in ('academic', 'general')),
  status text not null default 'in_progress' check (status in ('in_progress', 'completed', 'abandoned')),

  -- The exact published versions this mock is taken against, fixed at start.
  listening_version_id uuid not null references public.test_versions (id),
  reading_version_id uuid not null references public.test_versions (id),
  writing_version_id uuid not null references public.test_versions (id),
  speaking_version_id uuid not null references public.test_versions (id),

  -- Filled in as each module is started; each attempt belongs to at most one mock.
  listening_attempt_id uuid unique references public.attempts (id),
  reading_attempt_id uuid unique references public.attempts (id),
  writing_attempt_id uuid unique references public.attempts (id),
  speaking_attempt_id uuid unique references public.attempts (id),

  started_at timestamptz not null default now(),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint mock_attempts_completed_at_matches_status
    check ((status = 'completed') = (completed_at is not null))
);

comment on table public.mock_attempts is 'Full four-module mock (Listening -> Reading -> Writing -> Speaking). Pins one published test_version per module at start; frozen once completed or abandoned.';

-- FR-MOCK-04: one open mock per student, enforced by the database so two
-- concurrent "start" requests can't both create one.
create unique index mock_attempts_one_open_per_student
  on public.mock_attempts (student_id)
  where status = 'in_progress';

create index mock_attempts_student_idx on public.mock_attempts (student_id, started_at desc);

create or replace function public.touch_mock_attempts_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger mock_attempts_touch_updated_at
  before update on public.mock_attempts
  for each row
  execute function public.touch_mock_attempts_updated_at();

-- Same rule as attempts: the transition into completed/abandoned is
-- allowed, any later change is not. DELETE is left alone so deleting a
-- user (auth.users cascade) still works.
create or replace function public.prevent_terminal_mock_mutation()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if old.status != 'in_progress' then
    raise exception 'mock attempt % is % and no longer editable', old.id, old.status;
  end if;
  return new;
end;
$$;

create trigger mock_attempts_immutable_once_terminal
  before update on public.mock_attempts
  for each row
  execute function public.prevent_terminal_mock_mutation();

alter table public.mock_attempts enable row level security;

create policy "Own mock attempts are readable" on public.mock_attempts
  for select
  using (student_id = (select auth.uid()) or public.is_admin());

-- ---------------------------------------------------------------------
-- 2. certificates
-- ---------------------------------------------------------------------

create table public.certificates (
  id uuid primary key default gen_random_uuid(),
  -- Public, non-guessable identifier printed on the PDF (FR-CERT-02),
  -- e.g. PB-7KQ2-M9XD-4HWT-Z3NC (80 random bits).
  code text not null unique check (code ~ '^PB(-[A-Z0-9]{4}){4}$'),
  mock_attempt_id uuid not null unique references public.mock_attempts (id) on delete cascade,
  student_id uuid not null references auth.users (id) on delete cascade,
  issued_at timestamptz not null default now(),
  -- FR-CERT-08: revocation (admin only).
  revoked_at timestamptz,
  revoked_reason text,
  -- FR-CERT-06: public verification only with the holder's consent.
  public_verification boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.certificates is 'Unofficial practice-completion certificates, one per completed full mock. The PDF is generated on demand from server data and never stored.';

create index certificates_student_idx on public.certificates (student_id, issued_at desc);

create or replace function public.touch_certificates_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger certificates_touch_updated_at
  before update on public.certificates
  for each row
  execute function public.touch_certificates_updated_at();

-- The identity of a certificate never changes after issue: only the
-- consent flag and revocation fields may be updated.
create or replace function public.prevent_certificate_identity_change()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.code is distinct from old.code
     or new.mock_attempt_id is distinct from old.mock_attempt_id
     or new.student_id is distinct from old.student_id
     or new.issued_at is distinct from old.issued_at then
    raise exception 'certificate % identity fields are immutable', old.id;
  end if;
  return new;
end;
$$;

create trigger certificates_identity_immutable
  before update on public.certificates
  for each row
  execute function public.prevent_certificate_identity_change();

alter table public.certificates enable row level security;

create policy "Own certificates are readable" on public.certificates
  for select
  using (student_id = (select auth.uid()) or public.is_admin());
