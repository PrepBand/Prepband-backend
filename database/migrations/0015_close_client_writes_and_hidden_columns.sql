-- Code review fixes (2026-11-25, PR #2).
--
-- Applied 2026-11-25 to the live "PrepBand" Supabase project as migration
-- `close_client_writes_and_hidden_columns`. get_advisors(type: "security")
-- came back with only the known is_admin() exception and the pre-existing
-- auth_leaked_password_protection flag.
--
-- 1. Server-owned student tables. The `for all` ownership policies on
--    attempts, responses, writing_responses, speaking_responses and
--    self_reviews let a signed-in student write their own rows straight
--    through the Supabase Data API: change expires_at, raw_score or status
--    on an open attempt (the first change into 'submitted' passes the
--    frozen-attempt trigger), or write answers that skip the API's save
--    ordering and checks. Forged scores would then show in results and on
--    certificates. Only the backend (DATABASE_URL, which RLS doesn't apply
--    to) writes these tables; the frontend never writes them directly. So:
--    browsers keep read access to their own rows, and lose every write.
--    mock_attempts and certificates only had read policies already; their
--    write grants go too, so a later policy can't reopen them by accident.
--
-- 2. Results-only content. Listening transcripts (media_assets.transcript)
--    and Writing guidance (writing_prompts.examiner_notes) are shown only
--    after submission, but the published-content read policies exposed
--    every column to direct table reads. Browsers now get column-level
--    select on everything except those two columns. (A column revoke alone
--    doesn't work while a table-level grant exists - see 0013 - so the
--    table grant is revoked and the allowed columns granted one by one.)

-- --- 1. no direct writes to server-owned tables --------------------------

drop policy if exists "Own attempts are writable" on public.attempts;
drop policy if exists "Responses writable via owned attempt" on public.responses;

drop policy if exists "Writing responses via owned attempt" on public.writing_responses;
create policy "Writing responses readable via owned attempt" on public.writing_responses
  for select
  using (
    exists (
      select 1 from public.attempts a
      where a.id = writing_responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );

drop policy if exists "Speaking responses via owned attempt" on public.speaking_responses;
create policy "Speaking responses readable via owned attempt" on public.speaking_responses
  for select
  using (
    exists (
      select 1 from public.attempts a
      where a.id = speaking_responses.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );

drop policy if exists "Self reviews via owned attempt" on public.self_reviews;
create policy "Self reviews readable via owned attempt" on public.self_reviews
  for select
  using (
    exists (
      select 1 from public.attempts a
      where a.id = self_reviews.attempt_id
        and (a.student_id = (select auth.uid()) or public.is_admin())
    )
  );

revoke insert, update, delete, truncate, references, trigger
  on public.attempts,
     public.responses,
     public.writing_responses,
     public.speaking_responses,
     public.self_reviews,
     public.mock_attempts,
     public.certificates
  from anon, authenticated;

-- --- 2. hide results-only columns from direct reads ----------------------

do $$
declare
  target record;
  allowed text;
begin
  for target in
    select * from (values ('media_assets', 'transcript'), ('writing_prompts', 'examiner_notes')) as t(table_name, hidden)
  loop
    select string_agg(format('%I', column_name), ', ' order by ordinal_position)
      into allowed
      from information_schema.columns
     where table_schema = 'public'
       and table_name = target.table_name
       and column_name <> target.hidden;
    execute format('revoke select on public.%I from anon, authenticated', target.table_name);
    execute format('grant select (%s) on public.%I to anon, authenticated', allowed, target.table_name);
  end loop;
end;
$$;
