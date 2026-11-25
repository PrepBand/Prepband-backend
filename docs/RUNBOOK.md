# Prepband operations runbook

Deploying, checking, rolling back, backing up and restoring Prepband on its
free-tier stack. Spec sections 10, 13 and 14 (deliverable 8).

| Piece | Where | Name |
|---|---|---|
| Frontend | Netlify | `https://prepband.netlify.app`, continuous deploy from `PrepBand/Prepband-frontend` `main` |
| Backend | Render (free, Frankfurt) | service `prepband-backend` (`srv-dbuu3g6k1f9s73amjsig`), `https://prepband-backend.onrender.com` |
| Database + Auth | Supabase (free, eu-west-1) | project "PrepBand", ref `ztiuocjlthzvuonvwjzm` |
| Media | Cloudflare R2 | bucket `prepband-avatars` (avatars and `test-audio/`) |
| Email | Resend | sandbox sender until a domain is verified |

> Every change to any of these services needs the owner's explicit approval
> first (`Docs/rules.md`). Read-only checks don't.

---

## 1. Environment audit

Check these before every release. Values that are secrets never go in git.

### Render (`prepband-backend`)

| Variable | Secret | Value / rule |
|---|---|---|
| `DATABASE_URL` | yes | Supabase **session pooler** string, `postgresql+psycopg://postgres.ztiuocjlthzvuonvwjzm:<url-encoded password>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres`. Never the direct `db.<ref>` host (IPv6 only; Render is IPv4). |
| `CORS_ALLOWED_ORIGINS` | no | `https://prepband.netlify.app` (add preview origins only if needed) |
| `FRONTEND_URL` | no | `https://prepband.netlify.app` (certificate verification links) |
| `ENVIRONMENT` | no | `production`: this also hides `/docs` and `/openapi.json` |
| `PYTHON_VERSION` | no | `3.12.7` (Pillow 13 needs 3.11+) |
| `LOG_LEVEL` | no | `INFO` |
| `R2_ACCOUNT_ID` | no | `cedde90d9a63d3a3b418c1b3df7f57b2` (the account that holds `prepband-avatars` and `prepband-resources`) |
| `R2_ACCESS_KEY_ID` | yes | R2 API token, **Object Read & Write on `prepband-resources` only** |
| `R2_SECRET_ACCESS_KEY` | yes | Same token |
| `R2_RESOURCES_BUCKET` | no | `prepband-resources` |

Until the four `R2_*` values are set, the resource library works for links
but admins can't upload covers or files (the admin page says so) and file
resources can't be opened.

Build `pip install -r requirements.txt`; start
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`; health check path
`/api/v1/health`.

### Netlify (`prepband-frontend`)

| Variable | Secret | Value |
|---|---|---|
| `VITE_API_BASE_URL` | no | `https://prepband-backend.onrender.com/api/v1` |
| `VITE_SUPABASE_URL` | no | `https://ztiuocjlthzvuonvwjzm.supabase.co` |
| `VITE_SUPABASE_ANON_KEY` | no (public by design) | the project's publishable anon key |
| `VITE_APP_NAME` | no | `Prepband` |
| `VITE_VOSK_MODEL_URL` | no | optional override for the Vosk model |

`NODE_VERSION=22` is pinned in `netlify.toml`.

### Supabase

- Auth > URL configuration: Site URL `https://prepband.netlify.app`; redirect
  URLs `https://prepband.netlify.app/**`, `http://localhost:5180/**`.
- Auth > SMTP: Resend (`smtp.resend.com`, user `resend`).
- Auth > Email templates: "Confirm signup" **and "Reset Password"** must use
  `{{ .Token }}` (the reset template still uses a link as of 2026-11-25).
- Auth > Password security: turn on leaked-password protection (advisor warning).
- Edge Function secrets (`upload-avatar`, `upload-test-media`): `R2_ACCOUNT_ID`,
  `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_PUBLIC_BASE_URL`.

### Checks that must stay true

- No secret in the frontend bundle: `python -m scripts.smoke_test` checks.
- No secret in git history: CI runs gitleaks on every PR.
- Only `admin`/`super_admin` rows in `user_roles` can use `/admin`; roles change
  only with SQL (section 6).

---

## 2. Release

Order: **migrations, then backend, then frontend, then smoke test** (spec 13.6).
Migrations are additive, so the old app keeps working against the new schema.

1. **CI green** on the PR in both repos (lint, typecheck, tests, audit, secret scan).
2. **Migrations.** List what's live, then apply the missing files from
   `database/migrations/` in number order (Supabase MCP `apply_migration` or the
   SQL editor). Then run the security advisor; the only expected findings are
   `is_admin`, `is_email_registered` (both deliberately callable) and leaked-password
   protection.
3. **Edge Functions.** Redeploy `supabase/functions/upload-test-media` if it changed.
4. **Merge the backend PR to `main`**, then trigger the Render deploy by hand.
   Push-triggered auto-deploy has never been confirmed working, so don't wait
   for it.
5. Wait for `GET /api/v1/health/ready` to return `{"status":"ok","database":"ok"}`.
6. **Merge the frontend PR to `main`**. Netlify builds it automatically.
7. **Smoke test:** `python -m scripts.smoke_test` (read-only; section 4).
8. **Record the release** at the bottom of this file: date, both commit hashes,
   latest migration number, known issues.

### First release of Phases 2-6 (still to do)

Everything from Phase 2 onwards sits on branch `phase2-content-engine` in both
repos, **uncommitted**. Migrations 0000-0014 are all live (0011-0014
applied 2026-11-25; 0013 closed students' ability to rewrite their own
profile email; 0014 is the resource library). Steps 1 and 3-8 above remain,
plus:

- **finish the private resource bucket** (section 10): the bucket and its
  CORS rule exist; create its API token and set the four `R2_*` variables on
  Render;

- **deploy the frontend promptly:** 0011 revoked `is_email_registered` from
  browsers, and the live (Phase 1) forgot-password page calls it, so live
  password reset shows an error until the new frontend is deployed. The owner
  accepted this on 2026-11-25. The new backend needs 0011's `client_seq`
  columns and 0012's `attempts.question_marks`; both are in place;

- deploy the updated `upload-test-media` (examiner voice clips, Phase 5);
- publish "Listening Mock Test 1" from `/admin` (needed before any full mock can start);
- after the smoke test, run acceptance scenarios 1, 2, 5-10, 13 and 15 by hand
  on the live site (`Docs/LAUNCH_CHECKLIST.md`).

---

## 3. Turn on the CSP

`netlify.toml` sends `Content-Security-Policy-Report-Only`. After a release:

1. Open the live site with DevTools > Console open. Sign in, take a Reading and
   a Listening test, open the Speaking live examiner (let the models load),
   download a certificate, and use the admin panel, including an audio upload.
2. Look for `[Report Only] Refused to ...` messages. Add any legitimate origin
   to the matching directive.
3. When a full pass shows none, rename the header to `Content-Security-Policy`
   and release again.

---

## 4. Smoke test and cold start

```bash
cd prepband-backend
python -m scripts.smoke_test            # defaults to the production URLs
```

Read-only. It waits up to 90 s for a sleeping instance, then checks database
reachability, CORS (allowed and refused), 401s without a token, 404 for an
unknown certificate, API and frontend security headers, the SPA deep-link
fallback and secrets in the JS bundle.

**Cold start:** the free Render instance sleeps after about 15 minutes idle.
First observed live on 2026-11-25: **37.9 s** to wake. While waking, the
frontend shows "Waking up the server…" and retries GET/PUT requests for up to
90 s. POST/PATCH/DELETE are never repeated automatically.

---

## 5. Rollback

- **Frontend:** Netlify > Deploys > pick the previous deploy > "Publish deploy".
  Instant; no rebuild.
- **Backend:** Render > `prepband-backend` > Events > pick the previous deploy >
  "Rollback". Or `git revert` the merge on `main` and trigger a deploy.
- **Database:** migrations are additive, so an older app runs fine on a newer
  schema. **Don't** reverse a migration on the live database unless the
  migration itself is the problem. If it is, write a new forward migration that
  undoes it, review it, apply it.
- After any rollback, run the smoke test.

---

## 6. Admin accounts

Admin roles can't be granted from the app (spec 9). To promote someone, in the
Supabase SQL editor:

```sql
update public.user_roles set role = 'admin'
where user_id = (select id from auth.users where email = '<their email>');
```

Demote with `role = 'student'`. Every admin action is in `/admin/audit`.

---

## 7. Backups and restore

Supabase's free plan keeps daily backups you can't download, and there's no
point-in-time recovery. Make your own copy:

```bash
cd prepband-backend
python -m scripts.backup                 # pg_dump of public + every test as JSON
python -m scripts.backup --content-only  # without pg_dump installed
```

Output goes to `backups/<UTC timestamp>/` (gitignored: it contains student
data). Store it somewhere private. **Suggested cadence:** weekly, and always
before applying a migration.

**Restore content only** (lost or broken tests): Admin > Tests > Import JSON
with a file from `backups/<ts>/content/`, or
`python -m scripts.seed_content` after copying the files into `database/seeds/`.
Imports arrive as drafts; check them and publish.

**Full restore** (lost database). This has never been rehearsed, so plan for downtime:

1. Create a **new, empty** Supabase project (never restore over a live one).
2. Apply `database/migrations/0000`-latest in order. `0000_phase1_baseline.sql`
   holds the Phase 1 schema (`profiles`, `user_roles`, onboarding tables, RPCs),
   copied from the live migration history. The `tests/postgres` suite builds a
   database exactly this way on every CI run, so the folder is known to apply.
3. `psql "<new pooler url>" -f backups/<ts>/database.sql` loads the data.
4. Auth users are **not** in the `public` dump. They live in Supabase's `auth`
   schema and must be restored from Supabase's own backup (support request on
   paid plans) or re-created by users signing up again.
5. Point `DATABASE_URL`, `VITE_SUPABASE_*` and the Edge Function secrets at the
   new project, release, smoke test.

---

## 8. Free-tier quota monitoring

Check monthly, and when anything feels slow. Never upgrade a plan to get out
of trouble without the owner's decision.

| Service | Where to look | Limit to watch | If close |
|---|---|---|---|
| Supabase | Project > Usage | DB size 500 MB, egress, MAU; pauses after 7 days of no activity | Prune old attempts; any visit wakes a paused project (dashboard > Restore) |
| Render | Service > Metrics | 750 instance-hours/month, 512 MB RAM | Fine for one service; watch memory during PDF generation |
| Netlify | Team > Usage | Build minutes, bandwidth | Avoid unnecessary rebuilds |
| Cloudflare R2 | R2 > Overview | 10 GB storage, operation counts; r2.dev URL is rate-limited | Move to a custom domain before real launch |
| Resend | Dashboard | 100 emails/day; sandbox only sends to the owner until a domain is verified | Verify a domain before public launch |

The speech models download from Hugging Face, jsDelivr and GitHub straight to
students' browsers, so they cost nothing on our quotas.

**Keeping Supabase awake.** `.github/workflows/keepalive.yml` calls
`/api/v1/health/ready` (which queries the database) once a day. GitHub turns
scheduled workflows off after 60 days without a commit to the repo; if the
Actions tab shows it disabled, press "Enable workflow". Set the repository
variable `PREPBAND_API_URL` if the backend moves.

**Browser reports.** The frontend posts crashes and speech-model fallbacks to
`POST /api/v1/client-events`. They land in the Render log as lines starting
`client_event` (one JSON object each; no database, no third party). Search
Render > Logs for `client_event` weekly, and for `live_fallback` to see which
speech tiers fail on real devices. Render keeps free-plan logs for about 7 days.

---

## 9. Incidents

| Symptom | Likely cause | Do |
|---|---|---|
| "Waking up the server" for over 90 s | Render deploy failed or instance crashed | Render > Logs; roll back (section 5) |
| `/health` ok but `/health/ready` 503 | Database unreachable or paused | Supabase dashboard; restore a paused project; check `DATABASE_URL` |
| Sign-up/reset emails don't arrive | Resend sandbox or SMTP credentials | Supabase `auth_logs`; see `Docs/CLAUDE.md` "Email delivery" |
| Audio won't upload | Edge Function secrets missing | Supabase > Edge Functions > Secrets (section 1) |
| Many 429s | A script hammering the API, or limits too tight | Limits live in `app/services/rate_limit.py`; per instance, reset on restart |
| Unexpected admin action | Check `/admin/audit` (filter "Refused" too) | Demote the account (section 6) |
| Students report a white "Something went wrong" page | A browser crash | Render > Logs, search `client_event` for the page and stack |
| 409 "That change clashed with another one" | Two requests created the same row at once | Retryable; the browser retries saves. Many of them means a bug: check the log line's path |

Every API error carries a `correlation_id`; unexpected 500s log it on Render
with the stack trace, and the browser only ever sees the generic message.

---

## 10. Resource library storage (private R2 bucket)

Covers and files for the resource library live in their own **private**
bucket. Nothing in it is public: the API signs short-lived URLs (uploads 10
minutes, file links 5 minutes, covers 1 hour) and only for signed-in users.

One-time setup in Cloudflare. **Steps 1 and 3 were done on 2026-11-25**
(bucket created in APAC, `r2.dev` access off, no custom domain, CORS rule
below in place). Steps 2 and 4 remain for the owner, because the token's
secret must not pass through anyone else:

1. R2 > Create bucket `prepband-resources`. Leave **public access off** and
   don't enable the `r2.dev` URL.
2. R2 > Manage API tokens > Create token: *Object Read & Write*, scoped to
   `prepband-resources` only. Copy the access key id and secret into the
   four `R2_*` variables on Render (section 1).
3. Bucket > Settings > CORS policy, so admins' browsers can upload directly:

   ```json
   [
     {
       "AllowedOrigins": ["https://prepband.netlify.app", "http://localhost:5180"],
       "AllowedMethods": ["PUT", "GET"],
       "AllowedHeaders": ["content-type"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

4. Check: Admin > Resources no longer shows "File storage isn't configured",
   and uploading a cover shows its preview.

Limits enforced by the API: covers JPEG/PNG/WebP up to 5 MB (resized to
1200 px WebP in the browser first); files PDF, .docx, .pptx, JPEG, PNG,
WebP up to 25 MB. Every upload's first bytes are checked against its type
before it can be attached. Deleting or replacing a file deletes the old
object. Watch usage under R2 > Overview (free: 10 GB).

Search misses (what students looked for and didn't find) are logged as
`resource_search_miss` lines in the Render log: a free to-do list for new
resources.

---

## Release log

| Date | Frontend commit | Backend commit | Latest migration | Notes |
|---|---|---|---|---|
| 2026-11-19 | `main` (Phase 1) | `main` (Phase 1) | Phase 1 migrations | Phase 1 live. Later migrations (0001-0009) were applied ahead of the app code |
| _next_ | `phase2-content-engine` | `phase2-content-engine` | 0009 (applied 2026-11-25) | Phases 2-6, not yet deployed |
