# Prepband admin guide

For the people who write and publish content. Spec section 14, deliverable 7.
The admin panel is at **`/admin`**. There's no link to it from the student
app; type the address. Anyone who isn't an admin sees the normal 404 page.

## Getting access

An existing admin can't promote you from the app. The owner runs one SQL
statement (`docs/RUNBOOK.md` section 6). Sign out and back in afterwards.

## The content lifecycle

```
draft  ->  published  ->  archived
  ^            |              |
  |            +--Duplicate---+--> new draft copy
  +-- edit freely
```

- **Draft:** edit anything. Students can't see it.
- **Published:** frozen forever, so every past attempt stays reproducible.
  It shows on Practice (practice sets) or Mock tests (mocks).
- **Archived:** hidden from students; old attempts and results still work.
  **Restore** puts it back.
- **To change a published test:** Duplicate, edit the copy, publish the copy,
  archive the original. Students who already started keep the old version.

## Creating a test

1. Admin > Tests > **New test**: title, type (practice set or mock test),
   module, track, duration.
2. Add a section and fill it in:
   - **Reading:** one passage per section, then its questions.
   - **Listening:** upload the audio (MP3/WAV/OGG/M4A, up to 40 MB), paste the
     transcript (students see it only after submitting), then the questions.
   - **Writing:** Task 1 (with an optional data table or chart image) and Task 2.
   - **Speaking:** Parts 1-3. Part 2 needs a cue card. Add follow-up questions
     for the AI practice examiner, and optionally **Generate examiner voice**.
3. **Preview**, then check the **publish checklist**. It lists every problem
   at once: missing answer keys, Listening without uploaded audio, Reading
   without a passage, a Writing mock without both tasks, a Speaking mock
   without all three parts.
4. **Publish.**

### Question types and answers

| Type | Answer key |
|---|---|
| Multiple choice | exactly one of the options |
| Multiple select | one or more of the options |
| True / False / Not Given, Yes / No / Not Given | one of those values |
| Matching | one choice for every prompt. With several prompts in one question, each correct prompt earns its share of the marks |
| Short answer, sentence/note/form/table completion, gap fill | every accepted spelling. Add aliases for real alternatives ("10", "ten"). |

How text answers are marked:

- Ignored: capitals, extra spaces, end punctuation, hyphens ("pond dipping" =
  "pond-dipping"), ordinals ("19th" = "19"), thousands commas ("1,000" =
  "1000") and British/American spelling ("color" = "colour",
  "organize" = "organise"). You don't need aliases for these.
- **Word limit.** Set "Word limit" on a question (or `options: {"max_words": 2}`
  in JSON/CSV) and any longer answer is marked wrong, as in the exam. Numbers
  don't count as words; a hyphenated word counts as one. Imported questions
  get the limit automatically from their section's instructions ("NO MORE
  THAN TWO WORDS", "ONE WORD ONLY") unless the instructions name two
  different limits. Students see the limit under the answer box and a warning
  when they go over.
- **Multiple select** with several correct options earns marks per option;
  each wrong extra tick cancels one correct tick.

Write a rationale for every answer: students see it after submitting.

## Bulk work

- **Import / export a whole test:** Tests > Import JSON; Export on a test.
  Same format as `database/seeds/*.json`.
- **Questions by CSV** (Listening/Reading sections, drafts only): in a section,
  **Export CSV**, edit in a spreadsheet, **Import CSV**. "Adds questions" appends;
  "Replaces all" swaps the section's questions. A file with any bad row
  imports nothing and lists every problem with its row number.

  ```csv
  display_order,type,prompt,marks,options,correct_answers,accepted_aliases,rationale
  1,short_answer,Which city?,1,,London,the city of london,Paragraph B.
  2,multiple_choice,Pick one,1,Red|Green|Blue,Green,,
  3,matching,Match them,2,"{""prompts"":[""A"",""B""],""choices"":[""x"",""y""]}","{""0"":""x"",""1"":""y""}",,
  ```

  Separate list values with `|`; matching options and answers are JSON.
- **Several questions at once:** tick them, then **Set marks** or **Delete**.
  **Renumber** puts the section back in 1, 2, 3 order.
- **Several tests at once:** tick them on Admin > Tests, choose Publish,
  Archive, Restore or Delete drafts, and Apply. Each test follows the normal
  rules; any that can't be changed are listed with the reason.

## Analytics

Admin > **Analytics** shows activity by module and week and the busiest tests.
On a published test, **Analytics** shows each question's correct rate, how
often it was skipped, and the most common wrong answers. That's the place to
spot a broken answer key or a missing alias: fix it on a duplicate. Below 10
finished attempts, the rates are rough. Writing and Speaking show attempt
counts only, because they're never scored.

## Certificates

Admin > **Certificates** lists every issued practice certificate. **Revoke**
one (with a reason) if it's misused, for example presented as an official
result. The PDF then shows REVOKED, and public verification reports it as
revoked. Revoking can't be undone.

## The audit log

Admin > **Audit log** records every admin change (who, what, when, and
whether it worked), certificate revocations with their reason, exports, and
refused attempts by non-admins to reach admin pages. Nobody can edit or
delete entries.

## Resources

Admin > **Resources** is the library students see under Resources.

- **Categories** (bottom of the page): add, rename, reorder with the arrows,
  hide, or delete. Students only see visible categories that have published
  resources. A category with resources can't be deleted: move or delete them
  first, or hide it.
- **New resource**: title, short description, category, type, skill, track,
  optional level and tags (comma separated; they're searched).
  - **Type** decides what's attached: *Link* or *Video* take an https
    address; *PDF*, *Document* (.docx/.pptx) and *Image* take an uploaded file.
  - **Cover image** with **alt text** (what the picture shows, for
    screen-reader users). Big photos are shrunk automatically.
  - **Licence and source**: required to publish. Only original or properly
    licensed material; linking to someone else's page is fine, uploading
    their file isn't.
- **Create draft**, then **Publish** once the "Before publishing" list is
  empty. **Archive** hides a resource without deleting it. **Delete** removes
  it and its files for good.
- Files are private: a student gets a link that works for five minutes, so a
  link pasted elsewhere stops working. The list shows how many times each
  resource has been opened.

## Rules for content

- **Original or properly licensed material only.** Never copy real IELTS or
  Cambridge papers. Record the source, licence and attribution on every passage
  and audio file.
- **Never promise a band for Writing or Speaking**, in titles, instructions or
  rationales.
- Don't use the IELTS logo or imply Prepband is official.
- Generated Listening audio: `scripts/generate_listening_audio.ps1` (free
  Windows voices). Generated examiner voice: Kokoro, in the browser.

## Student accounts

Admin > **Users** shows names, emails, roles and attempt counts only: never
answers, passwords or private profile details. Account deletion requests come
through the Contact page; the owner deletes the user in Supabase
(Authentication > Users), which removes their profile and practice data with it.
