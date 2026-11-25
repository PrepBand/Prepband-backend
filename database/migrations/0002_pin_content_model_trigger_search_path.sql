-- Applied 2026-11-19 to the live "PrepBand" Supabase project as migration
-- `pin_content_model_trigger_search_path` (version 20261119191031).
--
-- get_advisors(type: "security") flagged both new trigger functions from
-- 0001_content_model.sql for a mutable search_path, the same hardening
-- is_admin() already has. Neither is SECURITY DEFINER, but pinning it is
-- still correct practice and cheap to fix.

alter function public.touch_tests_updated_at() set search_path = public;
alter function public.prevent_published_version_mutation() set search_path = public;
