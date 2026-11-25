-- Code review fixes (2026-11-25).
--
-- 1. Save ordering. Autosaves are retried while the free-tier server wakes
--    up, so an old save could arrive after a newer one and overwrite it.
--    The browser now numbers every change (client_seq, a millisecond
--    timestamp that only goes up) and the API ignores a save whose number is
--    not higher than the stored one. Nullable and additive: rows written
--    before this, and clients that don't send a number, behave as before.
--
-- 2. Account enumeration. is_email_registered(text) was callable by anon,
--    so anyone could check whether an email has an account. The forgot-
--    password page no longer calls it (it always says "if that email has an
--    account, we sent a code"). Execute is revoked from every client role;
--    the function is kept so nothing that still references it breaks, and
--    the service role can still use it.

alter table public.responses add column if not exists client_seq bigint;
alter table public.writing_responses add column if not exists client_seq bigint;
alter table public.speaking_responses add column if not exists client_seq bigint;

comment on column public.responses.client_seq is
  'Change number from the browser; the API ignores a save older than the stored one.';
comment on column public.writing_responses.client_seq is
  'Change number from the browser; the API ignores a save older than the stored one.';
comment on column public.speaking_responses.client_seq is
  'Change number from the browser; the API ignores a save older than the stored one.';

revoke execute on function public.is_email_registered(text) from public, anon, authenticated;
