-- Phase 2, Step 1: admin content model (tests, versions, sections, passages,
-- media assets, questions, answer keys).
--
-- Applied 2026-11-19 to the live "PrepBand" Supabase project (ref
-- ztiuocjlthzvuonvwjzm) as migration `content_model`
-- (version 20261119191003) - get_advisors(type: "security") came back
-- clean afterward except the two already-known, intentional exceptions
-- (is_admin() callable by anon/authenticated, same pattern as
-- is_email_registered; auth_leaked_password_protection, pre-existing and
-- dashboard-only). See 0002_pin_content_model_trigger_search_path.sql for
-- the one follow-up fix the advisor did flag as new. Written against the
-- schema already live there (profiles, user_roles) - see
-- prepband-backend/app/models.py and CLAUDE.md's "Database schema (live)"
-- section for the existing pattern this follows (UUID PKs, RLS
-- everywhere, SECURITY DEFINER helpers locked down to the minimum grant
-- they need).
--
-- Storage split (per the Cloudflare/Supabase architecture decision): this
-- migration only ever stores *metadata* about a binary asset
-- (media_assets.storage_key/public_url etc.) - the audio/image bytes
-- themselves live in the existing `prepband-avatars` R2 bucket, under new
-- prefixes alongside the existing `avatars/<user_id>/...` one:
--   test-audio/<test_version_id>/<media_asset_id>.<ext>
--   test-images/<test_version_id>/<media_asset_id>.<ext>
-- Nothing in this migration touches R2 - that's a separate, later step
-- (uploading real media once content authoring starts).

-- ---------------------------------------------------------------------
-- 1. tests (the editable/draft parent row; current_version_id is added
--    after test_versions exists, since the two tables reference each
--    other).
-- ---------------------------------------------------------------------

create table public.tests (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  module text not null check (module in ('listening', 'reading', 'writing', 'speaking')),
  track text not null check (track in ('academic', 'general', 'both')),
  difficulty text,
  duration_minutes integer,
  status text not null default 'draft' check (status in ('draft', 'published', 'archived')),
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.tests is 'Editable test metadata. The actual content students see comes from test_versions, never from live edits to this row once published.';

-- ---------------------------------------------------------------------
-- 2. test_versions (immutable once published - see the trigger below).
-- ---------------------------------------------------------------------

create table public.test_versions (
  id uuid primary key default gen_random_uuid(),
  test_id uuid not null references public.tests (id) on delete cascade,
  version_number integer not null,
  status text not null default 'draft' check (status in ('draft', 'published')),
  published_at timestamptz,
  created_at timestamptz not null default now(),
  unique (test_id, version_number)
);

comment on table public.test_versions is 'Frozen snapshot once status = published (FR-ADMIN-07): an attempt points at a specific version, so later edits to the test never change what an in-progress or completed attempt is graded against.';

alter table public.tests
  add column current_version_id uuid references public.test_versions (id) on delete set null;

-- ---------------------------------------------------------------------
-- 3. sections / passages / media_assets / questions / answer_keys
-- ---------------------------------------------------------------------

create table public.sections (
  id uuid primary key default gen_random_uuid(),
  test_version_id uuid not null references public.test_versions (id) on delete cascade,
  order_index integer not null,
  module text not null check (module in ('listening', 'reading', 'writing', 'speaking')),
  instructions text,
  timing_seconds integer,
  created_at timestamptz not null default now(),
  unique (test_version_id, order_index)
);

create table public.passages (
  id uuid primary key default gen_random_uuid(),
  section_id uuid not null references public.sections (id) on delete cascade,
  title text,
  body text not null,
  source text,
  license text,
  attribution text,
  created_at timestamptz not null default now()
);

create table public.media_assets (
  id uuid primary key default gen_random_uuid(),
  section_id uuid references public.sections (id) on delete cascade,
  type text not null check (type in ('audio', 'image')),
  storage_provider text not null default 'cloudflare_r2' check (storage_provider in ('cloudflare_r2')),
  storage_key text not null,
  public_url text,
  content_type text,
  duration_seconds numeric,
  size_bytes bigint,
  attribution text,
  license text,
  created_at timestamptz not null default now()
);

comment on column public.media_assets.storage_key is 'Object key inside the shared prepband-avatars R2 bucket, e.g. test-audio/<test_version_id>/<id>.mp3 - the bytes live in R2, this row is metadata only.';

create table public.questions (
  id uuid primary key default gen_random_uuid(),
  section_id uuid not null references public.sections (id) on delete cascade,
  type text not null check (
    type in (
      'multiple_choice', 'multiple_select', 'true_false_not_given', 'yes_no_not_given',
      'matching', 'short_answer', 'sentence_completion', 'gap_fill',
      'form_completion', 'note_completion', 'table_completion'
    )
  ),
  prompt text not null,
  options jsonb,
  display_order integer not null,
  marks numeric not null default 1,
  created_at timestamptz not null default now(),
  unique (section_id, display_order)
);

create table public.answer_keys (
  id uuid primary key default gen_random_uuid(),
  question_id uuid not null unique references public.questions (id) on delete cascade,
  correct_answers jsonb not null,
  accepted_aliases jsonb,
  rationale text,
  created_at timestamptz not null default now()
);

comment on table public.answer_keys is 'Never exposed to anon/authenticated via RLS (see below) - not readable by a student before they submit, per spec section 9.';

-- ---------------------------------------------------------------------
-- 4. updated_at touch trigger for tests (same pattern as profiles)
-- ---------------------------------------------------------------------

create or replace function public.touch_tests_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger tests_touch_updated_at
  before update on public.tests
  for each row
  execute function public.touch_tests_updated_at();

-- ---------------------------------------------------------------------
-- 5. Immutability: once a test_version is published, it can never be
--    updated or deleted again, including by an admin - a new edit means
--    creating a new draft version instead. This is enforced at the
--    database level, not just in the API, the same "don't trust the
--    client layer alone" instinct as the existing admin-role lockdown.
-- ---------------------------------------------------------------------

create or replace function public.prevent_published_version_mutation()
returns trigger
language plpgsql
as $$
begin
  if old.status = 'published' then
    raise exception 'test_versions row % is published and immutable', old.id;
  end if;
  return coalesce(new, old);
end;
$$;

create trigger test_versions_immutable_once_published
  before update or delete on public.test_versions
  for each row
  execute function public.prevent_published_version_mutation();

-- ---------------------------------------------------------------------
-- 6. is_admin() helper - reused by every RLS policy below instead of
--    repeating the user_roles subquery. SECURITY DEFINER so it can read
--    user_roles regardless of the caller's own row-level grants, but
--    EXECUTE must stay granted to authenticated (same deliberate
--    exception as is_email_registered) since RLS policies evaluate it
--    *as* the querying role, not as the function owner, when deciding
--    whether to call it at all.
-- ---------------------------------------------------------------------

create or replace function public.is_admin()
returns boolean
language sql
security definer
stable
set search_path = public
as $$
  select exists (
    select 1 from public.user_roles
    where user_id = (select auth.uid())
      and role in ('admin', 'super_admin')
  );
$$;

revoke all on function public.is_admin() from public;
grant execute on function public.is_admin() to anon, authenticated;

-- ---------------------------------------------------------------------
-- 7. RLS - students only ever see content through a published version
--    of a test; admins see and manage everything. answer_keys has no
--    select policy for anon/authenticated at all, deliberately: the
--    backend's DATABASE_URL connection uses the `postgres` role, which
--    bypasses RLS, so the API can still read it server-side - this lock
--    only matters for direct PostgREST/anon-key access, which is exactly
--    what must never see an answer before submission.
-- ---------------------------------------------------------------------

alter table public.tests enable row level security;
alter table public.test_versions enable row level security;
alter table public.sections enable row level security;
alter table public.passages enable row level security;
alter table public.media_assets enable row level security;
alter table public.questions enable row level security;
alter table public.answer_keys enable row level security;

create policy "Published tests are readable" on public.tests
  for select
  using (status = 'published' or public.is_admin());

create policy "Admins manage tests" on public.tests
  for all
  using (public.is_admin())
  with check (public.is_admin());

create policy "Published versions are readable" on public.test_versions
  for select
  using (
    status = 'published'
    or public.is_admin()
  );

create policy "Admins manage test_versions" on public.test_versions
  for all
  using (public.is_admin())
  with check (public.is_admin());

-- sections/passages/media_assets/questions all key off whether their
-- parent test_version is the test's current *published* version - a
-- published-but-superseded version (kept only so old attempts still
-- resolve) is intentionally not readable through this policy; only the
-- backend (RLS-bypassing DATABASE_URL role) resolves those for an
-- existing attempt.
create policy "Content of the current published version is readable" on public.sections
  for select
  using (
    public.is_admin()
    or exists (
      select 1 from public.tests t
      where t.current_version_id = sections.test_version_id
        and t.status = 'published'
    )
  );

create policy "Admins manage sections" on public.sections
  for all
  using (public.is_admin())
  with check (public.is_admin());

create policy "Content of the current published version is readable" on public.passages
  for select
  using (
    public.is_admin()
    or exists (
      select 1
      from public.sections s
      join public.tests t on t.current_version_id = s.test_version_id
      where s.id = passages.section_id
        and t.status = 'published'
    )
  );

create policy "Admins manage passages" on public.passages
  for all
  using (public.is_admin())
  with check (public.is_admin());

create policy "Content of the current published version is readable" on public.media_assets
  for select
  using (
    public.is_admin()
    or exists (
      select 1
      from public.sections s
      join public.tests t on t.current_version_id = s.test_version_id
      where s.id = media_assets.section_id
        and t.status = 'published'
    )
  );

create policy "Admins manage media_assets" on public.media_assets
  for all
  using (public.is_admin())
  with check (public.is_admin());

create policy "Content of the current published version is readable" on public.questions
  for select
  using (
    public.is_admin()
    or exists (
      select 1
      from public.sections s
      join public.tests t on t.current_version_id = s.test_version_id
      where s.id = questions.section_id
        and t.status = 'published'
    )
  );

create policy "Admins manage questions" on public.questions
  for all
  using (public.is_admin())
  with check (public.is_admin());

-- answer_keys: admin-only, full stop. No student-readable policy exists
-- at all, on purpose.
create policy "Admins manage answer_keys" on public.answer_keys
  for all
  using (public.is_admin())
  with check (public.is_admin());
