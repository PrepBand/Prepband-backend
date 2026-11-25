-- Phase 2 close-out: distinguish standalone practice sets from mock tests.
--
-- Before this, PracticePage and MockTestsPage both read the same published
-- catalog with nothing to tell them apart - MockTestsPage simply took the
-- most recently published test for a skill/track, so publishing a short
-- practice set after a full mock silently replaced the mock. `kind` makes
-- the distinction explicit and admin-controlled (spec §4.2: "one complete
-- original full mock per track, plus one standalone practice set for each
-- module", and FR-TEST-10: "clear distinction between individual
-- practice, module mock and full mock").
--
-- Additive only: existing rows default to 'practice'. Governed by the
-- existing tests RLS policies - no new policy needed.

alter table public.tests
  add column kind text not null default 'practice' check (kind in ('practice', 'mock'));

comment on column public.tests.kind is 'practice: shown on the Practice page. mock: shown on Mock tests and started in exam mode.';

create index tests_catalog_idx on public.tests (status, module, track, kind);
