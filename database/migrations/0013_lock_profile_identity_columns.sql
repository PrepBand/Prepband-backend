-- Security fix (2026-11-25, found by the new Postgres test suite).
--
-- Phase 1 (20261107132927) meant to stop students editing their own
-- profiles.email and email_confirmed_at with
--   revoke insert (email, ...), update (email, ...) on public.profiles ...
-- but Postgres ignores a column-level revoke while the role still holds the
-- table-level privilege, and Supabase grants table-level INSERT/UPDATE to
-- anon and authenticated by default. Checked live before this migration:
-- has_column_privilege('authenticated', 'public.profiles', 'email', 'UPDATE')
-- was true. So a signed-in student could set their profile email to anything
-- (RLS still kept it to their own row), including someone else's address,
-- which the unique constraint would then refuse to that person at sign-up.
--
-- Fix: take away the table-level privileges and grant UPDATE back only on
-- the columns the app writes as the student:
--   onboarding_completed   (frontend, useProfile.ts)
--   avatar_url             (upload-avatar Edge Function, runs with the
--                           student's token)
--   display_name, track, target_band, timezone, exam_date
--                          (student-editable profile fields)
-- email, email_confirmed_at and avatar_initials stay trigger-only
-- (handle_new_user, handle_user_email_sync), and so does INSERT: the signup
-- trigger creates every profile row.

revoke insert, update on public.profiles from anon, authenticated;

grant update (display_name, track, target_band, timezone, exam_date, onboarding_completed, avatar_url)
  on public.profiles to authenticated;
