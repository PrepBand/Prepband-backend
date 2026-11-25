-- Phase 1 baseline: the eight migrations applied to the live project in
-- November 2026, before numbered migrations lived in this repo. Copied
-- verbatim, in order, from supabase_migrations.schema_migrations on
-- 2026-11-25, so a fresh project (restore, or the Postgres test suite) can
-- be built from this folder alone. Already applied live: never re-run this
-- against the live project.
--
-- Note: 20261107142746 grants is_email_registered to anon/authenticated;
-- migration 0011 revokes that again (owner decision, 2026-11-25).

-- ===================================================================
-- 20261101163843 create_profiles_table
-- ===================================================================

-- Student profile, one row per auth user. Extra fields (track, target band,
-- timezone, exam date) are collected during onboarding (FR-AUTH-05/06).

create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text,
  track text check (track in ('academic', 'general')),
  target_band numeric(2, 1) check (target_band >= 0 and target_band <= 9),
  timezone text,
  exam_date date,
  avatar_initials text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.profiles is 'One row per auth.users id. Created automatically on signup via handle_new_user trigger.';

alter table public.profiles enable row level security;

create policy "Users can view own profile"
  on public.profiles for select
  to authenticated
  using (auth.uid() = id);

create policy "Users can update own profile"
  on public.profiles for update
  to authenticated
  using (auth.uid() = id)
  with check (auth.uid() = id);

create policy "Users can insert own profile"
  on public.profiles for insert
  to authenticated
  with check (auth.uid() = id);

-- Keep updated_at current on every row update.
create or replace function public.handle_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger set_profiles_updated_at
  before update on public.profiles
  for each row execute function public.handle_updated_at();

-- Auto-create a profile row whenever a new auth user is created, seeded
-- from the signUp() options.data.full_name passed by the frontend.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id, new.raw_user_meta_data ->> 'full_name');
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ===================================================================
-- 20261101163913 lock_down_handle_new_user_rpc
-- ===================================================================

-- handle_new_user() only needs to run as the on_auth_user_created trigger.
-- Triggers fire regardless of EXECUTE grants, so revoking public/API access
-- here removes the unwanted PostgREST RPC endpoint without breaking signup.
revoke execute on function public.handle_new_user() from public, anon, authenticated;

-- ===================================================================
-- 20261107132927 profiles_email_and_confirmation_status
-- ===================================================================

-- Add email + email-confirmation tracking to profiles, and auto-compute
-- avatar_initials from the signup full name (e.g. "John Smith" -> "JS").

alter table public.profiles
  add column email text,
  add column email_confirmed_at timestamptz;

-- Backfill existing rows from auth.users, including initials for anyone
-- who doesn't already have them set.
update public.profiles p
set email = u.email,
    email_confirmed_at = u.email_confirmed_at,
    avatar_initials = coalesce(
      p.avatar_initials,
      case
        when nullif(trim(u.raw_user_meta_data ->> 'full_name'), '') is null
          then upper(left(u.email, 1))
        when array_length(regexp_split_to_array(trim(u.raw_user_meta_data ->> 'full_name'), '\s+'), 1) = 1
          then upper(left(trim(u.raw_user_meta_data ->> 'full_name'), 1))
        else upper(
          left((regexp_split_to_array(trim(u.raw_user_meta_data ->> 'full_name'), '\s+'))[1], 1) ||
          left(
            (regexp_split_to_array(trim(u.raw_user_meta_data ->> 'full_name'), '\s+'))
              [array_length(regexp_split_to_array(trim(u.raw_user_meta_data ->> 'full_name'), '\s+'), 1)],
            1
          )
        )
      end
    )
from auth.users u
where u.id = p.id;

alter table public.profiles
  alter column email set not null,
  add constraint profiles_email_key unique (email);

-- Recreate the signup trigger function to also populate email,
-- email_confirmed_at, and computed avatar_initials.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path to ''
as $function$
declare
  v_full_name text := trim(coalesce(new.raw_user_meta_data ->> 'full_name', ''));
  v_parts text[] := regexp_split_to_array(v_full_name, '\s+');
  v_initials text;
begin
  if v_full_name = '' then
    v_initials := upper(left(new.email, 1));
  elsif array_length(v_parts, 1) = 1 then
    v_initials := upper(left(v_parts[1], 1));
  else
    v_initials := upper(left(v_parts[1], 1) || left(v_parts[array_length(v_parts, 1)], 1));
  end if;

  insert into public.profiles (id, display_name, email, email_confirmed_at, avatar_initials)
  values (new.id, new.raw_user_meta_data ->> 'full_name', new.email, new.email_confirmed_at, v_initials);
  return new;
end;
$function$;

-- Keep email / confirmation status in sync if they ever change later
-- (email change flow, or confirmation completing after insert).
create or replace function public.handle_user_email_sync()
returns trigger
language plpgsql
security definer
set search_path to ''
as $function$
begin
  if new.email is distinct from old.email or new.email_confirmed_at is distinct from old.email_confirmed_at then
    update public.profiles
    set email = new.email,
        email_confirmed_at = new.email_confirmed_at
    where id = new.id;
  end if;
  return new;
end;
$function$;

drop trigger if exists on_auth_user_email_change on auth.users;
create trigger on_auth_user_email_change
  after update on auth.users
  for each row
  execute function public.handle_user_email_sync();

-- Lock down both SECURITY DEFINER functions the same way handle_new_user
-- already was (triggers fire regardless of EXECUTE grants).
revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.handle_user_email_sync() from public, anon, authenticated;

-- email / email_confirmed_at are authoritative identity fields: only the
-- triggers above (which run as the function owner) may ever set them.
-- Block direct client writes even though the row-level "own profile"
-- policies would otherwise allow a user to forge their own email/status.
revoke insert (email, email_confirmed_at), update (email, email_confirmed_at)
  on public.profiles from anon, authenticated;

-- ===================================================================
-- 20261107133742 optimize_profiles_rls_initplan
-- ===================================================================

-- Supabase's performance linter (auth_rls_initplan) flags bare auth.uid()
-- calls in RLS policies: Postgres re-evaluates them per row instead of once
-- per statement. Wrapping in (select ...) lets the planner treat it as a
-- stable subquery/initplan. Same policies, same access rules, just cheaper
-- at scale. See https://supabase.com/docs/guides/database/postgres/row-level-security#call-functions-with-select

drop policy if exists "Users can view own profile" on public.profiles;
create policy "Users can view own profile"
  on public.profiles
  for select
  using ((select auth.uid()) = id);

drop policy if exists "Users can update own profile" on public.profiles;
create policy "Users can update own profile"
  on public.profiles
  for update
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

drop policy if exists "Users can insert own profile" on public.profiles;
create policy "Users can insert own profile"
  on public.profiles
  for insert
  with check ((select auth.uid()) = id);

-- ===================================================================
-- 20261107142746 add_is_email_registered_rpc
-- ===================================================================

-- Lets the forgot-password page distinguish "no account for this email"
-- from "code sent", at the user's explicit request. This is a deliberate,
-- confirmed trade-off AGAINST the project spec's anti-enumeration
-- requirement (FR-AUTH-04 / section 9) — see CLAUDE.md for the decision
-- record. Scope is deliberately minimal: a single boolean, nothing else
-- about the account is exposed.

create or replace function public.is_email_registered(p_email text)
returns boolean
language sql
security definer
set search_path = ''
stable
as $function$
  select exists (
    select 1 from auth.users where lower(email) = lower(p_email)
  );
$function$;

-- Unlike the trigger-only SECURITY DEFINER functions elsewhere in this
-- schema (which have EXECUTE revoked from anon/authenticated), this one is
-- MEANT to be called directly as a public RPC from the forgot-password
-- page, including by signed-out visitors. Do not "lock this down" to match
-- the other functions without re-checking with the user first.
grant execute on function public.is_email_registered(text) to anon, authenticated;

-- ===================================================================
-- 20261107162847 add_onboarding_profile_tables
-- ===================================================================

alter table public.profiles
  add column onboarding_completed boolean not null default false;

create table public.profile_about (
  user_id uuid primary key references auth.users(id) on delete cascade,
  first_name text not null,
  last_name text not null,
  gender text not null check (gender in ('male','female','prefer_not_to_say','other')),
  age smallint check (age is null or (age between 10 and 100)),
  phone_country_code text,
  phone_number text,
  city text not null,
  country text not null,
  avatar_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.profile_study_goals (
  user_id uuid primary key references auth.users(id) on delete cascade,
  exam_date date,
  ielts_type text not null check (ielts_type in ('academic','general')),
  preparation_level text not null check (preparation_level in ('beginner','intermediate','advanced')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.profile_target_scores (
  user_id uuid primary key references auth.users(id) on delete cascade,
  listening numeric(2,1) check (listening is null or (listening between 0 and 9)),
  reading numeric(2,1) check (reading is null or (reading between 0 and 9)),
  writing numeric(2,1) check (writing is null or (writing between 0 and 9)),
  speaking numeric(2,1) check (speaking is null or (speaking between 0 and 9)),
  overall numeric(2,1) check (overall is null or (overall between 0 and 9)),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.profile_about enable row level security;
alter table public.profile_study_goals enable row level security;
alter table public.profile_target_scores enable row level security;

create policy "Users can select own about" on public.profile_about
  for select using ((select auth.uid()) = user_id);
create policy "Users can insert own about" on public.profile_about
  for insert with check ((select auth.uid()) = user_id);
create policy "Users can update own about" on public.profile_about
  for update using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

create policy "Users can select own study goal" on public.profile_study_goals
  for select using ((select auth.uid()) = user_id);
create policy "Users can insert own study goal" on public.profile_study_goals
  for insert with check ((select auth.uid()) = user_id);
create policy "Users can update own study goal" on public.profile_study_goals
  for update using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

create policy "Users can select own target scores" on public.profile_target_scores
  for select using ((select auth.uid()) = user_id);
create policy "Users can insert own target scores" on public.profile_target_scores
  for insert with check ((select auth.uid()) = user_id);
create policy "Users can update own target scores" on public.profile_target_scores
  for update using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

create trigger set_profile_about_updated_at before update on public.profile_about
  for each row execute function handle_updated_at();
create trigger set_profile_study_goals_updated_at before update on public.profile_study_goals
  for each row execute function handle_updated_at();
create trigger set_profile_target_scores_updated_at before update on public.profile_target_scores
  for each row execute function handle_updated_at();

-- ===================================================================
-- 20261108172234 add_avatar_url_to_profiles
-- ===================================================================

alter table public.profiles
  add column avatar_url text null;

comment on column public.profiles.avatar_url is 'Public R2 URL of the user''s uploaded profile photo. Set only by the upload-avatar Edge Function; null means fall back to avatar_initials.';

-- ===================================================================
-- 20261118144643 add_user_roles_table
-- ===================================================================

-- Student/admin role separation (Phase 1: backend protected-route foundation).
-- Deliberately a separate table from profiles, per the project spec's
-- user_roles design, so role is never writable through the existing
-- owner-only profiles insert/update policies.
create table public.user_roles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null default 'student' check (role in ('student', 'admin', 'super_admin')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.user_roles is 'One row per auth.users id. Role defaults to student on signup via handle_new_user_role trigger; only ever changed by direct SQL (super_admin/admin cannot be self-assigned from the client).';

alter table public.user_roles enable row level security;

-- Users may read their own role (so the frontend/backend can show/gate UI),
-- but have no insert/update/delete policy at all: the only write path is the
-- SECURITY DEFINER trigger below, which bypasses RLS as the function owner.
create policy "Users can view own role"
  on public.user_roles
  for select
  using ((select auth.uid()) = user_id);

create trigger set_user_roles_updated_at
  before update on public.user_roles
  for each row
  execute function public.handle_updated_at();

create or replace function public.handle_new_user_role()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.user_roles (user_id, role)
  values (new.id, 'student');
  return new;
end;
$$;

create trigger on_auth_user_created_role
  after insert on auth.users
  for each row
  execute function public.handle_new_user_role();

-- Lock down, same pattern as handle_new_user: triggers fire regardless of
-- EXECUTE grants, but the function must not be callable directly as a
-- public RPC (Supabase's security advisor flags SECURITY DEFINER functions
-- auto-exposed to PostgREST).
revoke execute on function public.handle_new_user_role() from public, anon, authenticated;
