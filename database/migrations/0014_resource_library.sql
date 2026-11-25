-- Resource library (2026-11-25): guides, sample answers, worksheets and
-- links in admin-managed categories, searchable, for signed-in students.
--
-- Files and cover images live in a PRIVATE Cloudflare R2 bucket. Rows keep
-- only the object key; the API hands out short-lived signed URLs, to
-- signed-in students only. Nothing here is public.
--
-- Writes go through the API (audited by the admin middleware). Browsers get
-- read access to published rows only, and no write privileges at all:
-- Supabase grants table-level INSERT/UPDATE/DELETE to anon and authenticated
-- by default, so those are revoked explicitly (migration 0013 showed RLS
-- alone isn't the whole story).

-- pg_trgm gives typo-tolerant title search. Supabase and the CI Postgres
-- have it; a bare local Postgres may not, so it is optional here and the API
-- falls back to plain full-text search without it.
create schema if not exists extensions;
do $$
begin
  if exists (select 1 from pg_available_extensions where name = 'pg_trgm') then
    create extension if not exists pg_trgm with schema extensions;
  end if;
end
$$;

-- --- categories ----------------------------------------------------------

create table public.resource_categories (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 2 and 60),
  slug text not null unique check (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  description text check (description is null or char_length(description) <= 300),
  display_order integer not null default 0,
  is_visible boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.resource_categories is
  'One level of admin-managed categories for the resource library. Hidden categories hide their resources from students.';

create index resource_categories_order_idx on public.resource_categories (display_order, name);

-- --- resources -------------------------------------------------------------

create table public.resources (
  id uuid primary key default gen_random_uuid(),
  category_id uuid not null references public.resource_categories (id) on delete restrict,
  title text not null check (char_length(title) between 3 and 140),
  description text check (description is null or char_length(description) <= 1000),
  kind text not null check (kind in ('link', 'video', 'pdf', 'document', 'image')),
  module text not null default 'general' check (module in ('listening', 'reading', 'writing', 'speaking', 'general')),
  track text not null default 'both' check (track in ('academic', 'general', 'both')),
  level text check (level is null or level in ('beginner', 'intermediate', 'advanced')),
  tags text[] not null default '{}' check (cardinality(tags) <= 12),
  -- Cover image (private R2 object) and its alt text.
  cover_key text check (cover_key is null or cover_key ~ '^resources/covers/[0-9a-f-]{36}\.(jpg|png|webp)$'),
  cover_alt text check (cover_alt is null or char_length(cover_alt) <= 200),
  -- Either a file (private R2 object) or an external https link.
  file_key text check (file_key is null or file_key ~ '^resources/files/[0-9a-f-]{36}\.[a-z0-9]{2,5}$'),
  file_name text check (file_name is null or char_length(file_name) <= 200),
  mime_type text,
  size_bytes bigint check (size_bytes is null or size_bytes > 0),
  external_url text check (external_url is null or (external_url ~ '^https://' and char_length(external_url) <= 2000)),
  -- Content rule: only original or properly licensed material.
  source text check (source is null or char_length(source) <= 300),
  licence text check (licence is null or char_length(licence) <= 120),
  status text not null default 'draft' check (status in ('draft', 'published', 'archived')),
  display_order integer not null default 0,
  open_count integer not null default 0,
  created_by uuid references auth.users (id) on delete set null,
  published_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- Kept by the trigger below: title (A), tags (B), description (C),
  -- category name (D). A generated column can't call array_to_string.
  search tsvector,
  -- A published resource is complete: cover with alt text, and the thing
  -- itself (a link for link/video, a file otherwise).
  constraint resources_published_complete check (
    status <> 'published' or (
      cover_key is not null and coalesce(char_length(cover_alt), 0) > 0
      and case when kind in ('link', 'video') then external_url is not null else file_key is not null end
    )
  )
);

comment on table public.resources is
  'Resource library entries. Files and covers are private R2 objects served through short-lived signed URLs by the API.';

create index resources_category_idx on public.resources (category_id);
create index resources_listing_idx on public.resources (status, display_order, published_at desc);
create index resources_created_by_idx on public.resources (created_by);
create index resources_search_idx on public.resources using gin (search);
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_trgm') then
    execute 'create index resources_title_trgm_idx on public.resources using gin (title extensions.gin_trgm_ops)';
  end if;
end
$$;

-- --- search vector + updated_at ---------------------------------------------

create or replace function public.resources_refresh_search()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  category_name text;
begin
  select name into category_name from public.resource_categories where id = new.category_id;
  new.search :=
    setweight(to_tsvector('english', coalesce(new.title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(array_to_string(new.tags, ' '), '')), 'B') ||
    setweight(to_tsvector('english', coalesce(new.description, '')), 'C') ||
    setweight(to_tsvector('english', coalesce(category_name, '')), 'D');
  return new;
end;
$$;

-- Only when searchable text changes (not on open_count bumps); the API
-- sets updated_at itself.
create trigger resources_refresh_search
  before insert or update of title, tags, description, category_id on public.resources
  for each row execute function public.resources_refresh_search();

create or replace function public.resource_categories_touch()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger resource_categories_touch
  before update on public.resource_categories
  for each row execute function public.resource_categories_touch();

-- Renaming a category re-indexes its resources (after the rename is
-- visible, so the search vector picks up the new name).
create or replace function public.resource_categories_reindex()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.name is distinct from old.name then
    update public.resources set title = title where category_id = new.id;
  end if;
  return null;
end;
$$;

create trigger resource_categories_reindex
  after update on public.resource_categories
  for each row execute function public.resource_categories_reindex();

revoke execute on function public.resources_refresh_search() from public, anon, authenticated;
revoke execute on function public.resource_categories_touch() from public, anon, authenticated;
revoke execute on function public.resource_categories_reindex() from public, anon, authenticated;

-- --- access ------------------------------------------------------------------

alter table public.resource_categories enable row level security;
alter table public.resources enable row level security;

-- Supabase's default grants give the API roles everything (even TRUNCATE).
-- Start from nothing and grant back only what's needed: signed-in reads.
revoke all on public.resource_categories, public.resources from anon, authenticated;
grant select on public.resource_categories, public.resources to authenticated;

create policy "Signed-in users read visible categories"
  on public.resource_categories for select
  to authenticated
  using (is_visible);

create policy "Signed-in users read published resources"
  on public.resources for select
  to authenticated
  using (
    status = 'published'
    and exists (select 1 from public.resource_categories c where c.id = category_id and c.is_visible)
  );

-- --- starting categories -----------------------------------------------------

insert into public.resource_categories (name, slug, description, display_order) values
  ('Strategy guides', 'strategy-guides', 'How to approach each part of the test.', 10),
  ('Sample answers', 'sample-answers', 'Model answers with notes on what makes them work.', 20),
  ('Vocabulary', 'vocabulary', 'Topic word lists and collocations.', 30),
  ('Grammar', 'grammar', 'The structures that matter most for IELTS.', 40),
  ('Band descriptors', 'band-descriptors', 'What examiners look for at each band.', 50),
  ('Practice worksheets', 'practice-worksheets', 'Printable exercises.', 60),
  ('Exam-day tips', 'exam-day-tips', 'What to bring, timing and staying calm.', 70)
on conflict (slug) do nothing;
