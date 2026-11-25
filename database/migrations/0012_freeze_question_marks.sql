-- Freeze per-question marks at submit (2026-11-25).
--
-- attempts.raw_score is frozen when an attempt ends, but the results page
-- re-marked each question on every view with the current grader. A change
-- to the marking rules (word limits, spelling variants) could then make old
-- results disagree with the saved total. The API now writes
-- {question_id: marks} here in the same update that ends the attempt, and
-- the results page reads it. The existing attempts_immutable_once_terminal
-- trigger keeps it frozen afterwards, like every other column.
--
-- Nullable and additive: attempts finished before this have null and keep
-- being marked on view; the running backend ignores the column.

alter table public.attempts add column if not exists question_marks jsonb;

comment on column public.attempts.question_marks is
  'Marks per question ({question_id: marks}) frozen at submit; null for attempts finished before migration 0012.';
