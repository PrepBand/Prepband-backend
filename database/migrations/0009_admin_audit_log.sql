-- Phase 6: admin audit trail (FR-ADMIN-08, spec section 9 "record
-- sensitive admin actions").
--
-- One append-only row per admin write through the API (and per refused
-- attempt to reach an admin route). Rows are written by the backend's own
-- server-side database connection, never by the browser, so there is no
-- insert policy for clients at all. Admins can read the log; nobody can
-- edit a row once written.

create table public.admin_audit_log (
  id uuid primary key default gen_random_uuid(),
  -- Deliberately no foreign key: an `on delete set null` would be an
  -- UPDATE (blocked below), and the log should outlive deleted accounts.
  actor_id uuid,
  actor_role text check (actor_role is null or actor_role in ('student', 'admin', 'super_admin')),
  action text not null check (length(action) between 1 and 200),
  method text not null check (method in ('POST', 'PUT', 'PATCH', 'DELETE', 'GET')),
  path text not null check (length(path) <= 500),
  entity_type text check (entity_type is null or length(entity_type) <= 60),
  entity_id text check (entity_id is null or length(entity_id) <= 100),
  outcome text not null check (outcome in ('success', 'denied', 'failed')),
  status_code integer not null check (status_code between 100 and 599),
  details jsonb not null default '{}'::jsonb check (jsonb_typeof(details) = 'object'),
  created_at timestamptz not null default now()
);

comment on table public.admin_audit_log is 'Append-only record of admin writes and refused admin access (FR-ADMIN-08). Written only by the backend; readable by admins; rows can never be updated.';

-- Newest-first listing, and filtering by who did it / what it touched.
create index admin_audit_log_created_at_idx on public.admin_audit_log (created_at desc);
create index admin_audit_log_actor_id_idx on public.admin_audit_log (actor_id, created_at desc);
create index admin_audit_log_entity_idx on public.admin_audit_log (entity_type, entity_id);

alter table public.admin_audit_log enable row level security;

create policy "Admins read the audit log" on public.admin_audit_log
  for select
  to authenticated
  using ((select public.is_admin()));

-- No insert/update/delete policies: clients can never write here.

create or replace function public.prevent_admin_audit_log_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  raise exception 'admin_audit_log rows are append-only';
end;
$$;

revoke execute on function public.prevent_admin_audit_log_update() from public, anon, authenticated;

create trigger admin_audit_log_append_only
  before update on public.admin_audit_log
  for each row
  execute function public.prevent_admin_audit_log_update();
