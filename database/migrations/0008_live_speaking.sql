-- Phase 5: live AI speaking examiner (FR-LIVE-01..12).
--
-- Additive only. The live examiner runs in the student's browser (speech
-- recognition, examiner voice, turn-taking and the optional follow-up
-- model are all client-side, free models); the database only stores the
-- examiner's script and, with the student's consent, the transcript.
--
--   speaking_prompts.follow_ups   admin-written follow-up questions the
--                                 examiner may ask after this prompt:
--                                 [{"text": "...", "audio_url": "https://..."|null}]
--   speaking_prompts.audio_url    optional pre-generated examiner voice clip
--   speaking_responses            response_mode 'live' + transcript /
--                                 stt_model / turns (consent-gated by the API)
--   examiner_phrases              the examiner's fixed lines (greeting,
--                                 part introductions, "Mm-hm", "Take your
--                                 time", ...), editable by admins

-- ---------------------------------------------------------------------
-- 1. Speaking prompts: follow-ups and examiner audio
-- ---------------------------------------------------------------------

alter table public.speaking_prompts
  add column follow_ups jsonb not null default '[]'::jsonb
    check (jsonb_typeof(follow_ups) = 'array' and jsonb_array_length(follow_ups) <= 10),
  add column audio_url text check (audio_url is null or audio_url ~ '^https://');

comment on column public.speaking_prompts.follow_ups is 'Admin-authored follow-ups the live examiner may ask, [{text, audio_url}]. A follow-up model may only choose among these, never invent new ones (FR-LIVE-09).';
comment on column public.speaking_prompts.audio_url is 'Optional pre-generated examiner voice clip for this prompt (Kokoro, generated in the admin panel).';

-- ---------------------------------------------------------------------
-- 2. Speaking responses: live mode and consented transcripts
-- ---------------------------------------------------------------------

alter table public.speaking_responses
  drop constraint speaking_responses_response_mode_check;

alter table public.speaking_responses
  add constraint speaking_responses_response_mode_check
    check (response_mode in ('audio', 'text', 'live'));

alter table public.speaking_responses
  add column transcript text check (transcript is null or length(transcript) <= 20000),
  add column stt_model text check (stt_model is null or length(stt_model) <= 80),
  add column turns jsonb check (turns is null or (jsonb_typeof(turns) = 'array' and jsonb_array_length(turns) <= 20));

comment on column public.speaking_responses.transcript is 'Live mode only, and only when the student consented to saving it (FR-LIVE-11). The audio itself is never stored.';
comment on column public.speaking_responses.turns is 'Live mode: one entry per examiner question asked for this prompt (main question and follow-ups) with durations and pause counts; transcripts inside only with consent.';

-- ---------------------------------------------------------------------
-- 3. Examiner phrase bank
-- ---------------------------------------------------------------------

create table public.examiner_phrases (
  key text primary key check (key ~ '^[a-z0-9_]{2,60}$'),
  text text not null check (length(text) between 1 and 500),
  audio_url text check (audio_url is null or audio_url ~ '^https://'),
  updated_at timestamptz not null default now()
);

comment on table public.examiner_phrases is 'Fixed lines the live AI practice examiner says. Edited by admins; read by every signed-in student.';

create or replace function public.touch_examiner_phrases_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger examiner_phrases_touch_updated_at
  before update on public.examiner_phrases
  for each row
  execute function public.touch_examiner_phrases_updated_at();

alter table public.examiner_phrases enable row level security;

create policy "Signed-in users read examiner phrases" on public.examiner_phrases
  for select
  to authenticated
  using (true);

create policy "Admins manage examiner phrases" on public.examiner_phrases
  for all
  to authenticated
  using (public.is_admin())
  with check (public.is_admin());

insert into public.examiner_phrases (key, text) values
  ('greeting', 'Hello. I''m your AI practice examiner for this speaking test. It has three parts, just like the real exam.'),
  ('ask_name', 'Can you tell me your full name, please?'),
  ('ask_origin', 'Thank you. And where are you from?'),
  ('part1_intro', 'Now, in this first part, I''d like to ask you some questions about yourself.'),
  ('part2_intro', 'Now I''m going to give you a topic, and I''d like you to talk about it for one to two minutes. Before you talk, you''ll have one minute to think about what you''re going to say. You can make some notes if you wish.'),
  ('part2_start', 'All right? Remember, you have one to two minutes for this, so don''t worry if I stop you. I''ll tell you when the time is up. Can you start speaking now, please?'),
  ('part2_stop', 'Thank you.'),
  ('part3_intro', 'We''ve been talking about this topic. Now I''d like to discuss with you some more general questions related to it.'),
  ('ack_1', 'Mm-hm.'),
  ('ack_2', 'I see.'),
  ('ack_3', 'Okay.'),
  ('ack_4', 'Right.'),
  ('more', 'Could you tell me a little more about that?'),
  ('part2_more', 'Is there anything else you can tell me about it?'),
  ('why', 'Why do you think that is?'),
  ('example', 'Can you give me an example?'),
  ('take_time', 'Take your time.'),
  ('repeat_intro', 'Of course. I''ll repeat the question.'),
  ('no_answer', 'That''s all right. Let''s try another question.'),
  ('time_up', 'Thank you.'),
  ('move_on', 'Thank you. Let''s move on.'),
  ('end', 'Thank you. That is the end of the speaking test.');
