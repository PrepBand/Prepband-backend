# prepband-backend

The FastAPI backend for **Prepband** — a free-tier, fully-hosted,
open-source IELTS-style practice platform. **Not affiliated with IELTS.**
Scaffolded as Phase 1 work (spec section 11: "foundation, auth and design
system" — specifically the "protected API middleware" and
"student/admin separation proven" pieces).

See `../CLAUDE.md` (source of truth for what's actually built and why) and
`../IELTS_Preparation_Platform_Full_Project_Scope.md` (the full product
spec) in the parent `PrepBand/` directory before making changes here.

## Tech stack

| Layer | Choice |
|---|---|
| Framework | FastAPI |
| ORM | SQLModel (SQLAlchemy 2.0 + Pydantic v2) |
| DB driver | psycopg v3 (`psycopg[binary]`) |
| Auth | Supabase Auth JWTs, verified locally via `PyJWKClient` against the project's live JWKS endpoint (ES256) — no service-role key needed for this |
| Settings | `pydantic-settings` |
| Server | Uvicorn |
| Hosting | Render (free-tier web service, Frankfurt region) |

## Architecture

This repo is one of two independently deployed apps that make up Prepband
(the other is [`prepband-frontend`](../prepband-frontend), a separate git
repo under the same local `PrepBand/` folder — not a monorepo, by design).

```text
Client (browser, or any Bearer-token holder)
  │
  ├──> Render (this app)
  │       │
  │       ├──> Supabase Auth JWKS (public, no secret) — verifies the
  │       │    caller's access token's signature/issuer/audience/expiry.
  │       │    Identity always comes from the verified token's `sub`
  │       │    claim, never a client-supplied id (FR-AUTH-07).
  │       │
  │       └──> Supabase Postgres, via the Supavisor **session pooler**
  │            (aws-0-eu-west-1.pooler.supabase.com:5432, IPv4) — NOT the
  │            "direct connection" host, which is IPv6-only and explicitly
  │            documented by Supabase as broken on Render. DATABASE_URL
  │            must use this pooler host or the app can't reach the DB.
  │
  └──  (nothing yet calls this from prepband-frontend — VITE_API_BASE_URL
       is set but unused there; see that repo's README)
```

Schema (tables, RLS policies, triggers) is **owned by Supabase migrations**,
applied via the Supabase MCP and mirrored into
`prepband-frontend/src/lib/database.types.ts` — this backend only ever
reads/writes through SQLModel models that mirror that schema; it never
runs `SQLModel.metadata.create_all()` or owns any migration of its own.

## Project structure

```text
app/
  main.py            # app creation, CORS, exception handlers, router mounting
  core/
    config.py        # pydantic-settings; non-secret Supabase URLs default here
    security.py      # get_current_user_id — real JWT verification via JWKS
    roles.py          # get_current_role / require_admin — role-gate dependency
    errors.py         # consistent {"error": {code, message, correlation_id, retryable}} envelope
  db/
    session.py        # SQLModel engine/session from DATABASE_URL
  models.py            # SQLModel mappings for the existing profiles / user_roles tables
  schemas/
    profile.py         # Pydantic request/response schemas for /me
  api/v1/
    health.py          # GET /health (public)
    me.py               # GET/PATCH /me (authenticated)
    admin.py            # GET /admin/ping (authenticated + admin-gated)
    router.py           # combines the above
```

## `user_roles` and the admin/student gate

Migration `add_user_roles_table` added `public.user_roles` (`user_id`,
`role` defaulting to `'student'`, checked against
`'student' | 'admin' | 'super_admin'`). A `handle_new_user_role` trigger
(locked down the same way as `handle_new_user`: `SECURITY DEFINER`,
`EXECUTE` revoked from `anon`/`authenticated`) inserts a `student` row on
signup. RLS only grants `select` on your own row — there is deliberately
no insert/update/delete policy, so a role can only ever be changed by
someone running SQL directly (e.g. via the Supabase MCP or dashboard),
never through the API or frontend. To make yourself an admin for local
testing:

```sql
update public.user_roles set role = 'admin' where user_id = '<your auth.users id>';
```

This has been verified end-to-end against a real (since deleted) test
account: a plain signup gets `role: student` and a 403 from
`/admin/ping`; after the SQL update above, the same token gets
`role: admin` and a 200.

## What this backend deliberately does *not* do

- **No avatar/R2 upload endpoint.** An earlier prototype here signed
  presigned R2 PUT URLs, but nothing ever called it, and Prepband's real
  avatar upload now goes through a Supabase Edge Function
  (`upload-avatar`) that talks to R2 directly — see `../CLAUDE.md`'s
  "Avatar storage (Cloudflare R2)" section. That prototype has been
  removed; this backend has no R2 credentials and shouldn't gain any.
- **No stored certificate files.** Certificate PDFs (Phase 4) are rendered
  on demand with ReportLab from the server's own attempt records and
  streamed back; nothing is written to storage.

## Full mock and certificates (Phase 4)

- `POST /mocks` starts a full mock (or returns the open one), pinning the
  newest published *mock* test version per module for the track.
  `POST /mocks/{id}/modules/{module}/start` starts or resumes one section in
  order (Listening → Reading → Writing → Speaking) as an exam-mode attempt.
  `GET /mocks/{id}` reconciles expired sections and completes the mock once
  all four are submitted or expired; `POST /mocks/{id}/complete` does the
  same explicitly, and `POST /mocks/{id}/abandon` ends it early.
  `GET /mocks/{id}/results` gives consolidated results: the overall band
  is always "Not available" while Writing/Speaking are unassessed.
- Completing a mock issues exactly one certificate (`PB-XXXX-XXXX-XXXX-XXXX`).
  `GET /certificates`, `GET /certificates/{id}`, `GET /certificates/{id}/pdf`
  (rate-limited), `PATCH /certificates/{id}` (public-verification consent
  only). `GET /verify/{code}` is public and shows only ID, validity and issue
  date for consented certificates. Admins: `GET /admin/certificates`,
  `POST /admin/certificates/{id}/revoke`.
- `GET /attempts/{id}/content` returns the exact version an attempt was
  started against; `GET /me/attempts` is filtered and paginated;
  `GET /me/progress` feeds the progress charts.

## Tests and lint

```bash
pip install -r requirements-dev.txt
python -m ruff check .   # lint (ruff.toml)
python -m pytest         # API tests on SQLite, plus tests/postgres when a Postgres is available
```

The main suite runs the real app against a throwaway in-memory SQLite
database with JWT verification replaced by a test header (`tests/conftest.py`).

`tests/postgres` builds a fresh Postgres database from `database/migrations`
(0000 is the Phase 1 baseline) plus a small Supabase stub, then checks what
SQLite can't: triggers, row-level security, grants, account deletion and the
row locks under simultaneous requests. It uses `TEST_DATABASE_URL` (CI starts
a `postgres:17` service) or, locally, `pip install pgserver` for a throwaway
server. Without either, those tests are skipped.

## Local dev

```bash
cd prepband-backend
python -m venv .venv
. .venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env       # fill in DATABASE_URL before using /me or /admin/ping
uvicorn app.main:app --reload --port 8000
```

`GET /health` works immediately, no `.env` needed. `/me` and `/admin/ping`
need `DATABASE_URL` filled into `.env` first; until then they return a
clear `503 "Database is not configured yet."` rather than a stack trace.
**Use the Supabase dashboard's "Session pooler" connection string, not
"Direct connection"** — see the Architecture section above for why — and
percent-encode any special character in the password (e.g. `@` → `%40`).

`CORS_ALLOWED_ORIGINS` must include the frontend's origin
(`http://localhost:5180` for local dev, `https://prepband.netlify.app` in
production — already set on the deployed Render service) for the frontend
to call this API from the browser.

## Admin operations and security (Phase 6)

- **Audit trail:** every admin write, sensitive read and refused admin access
  is recorded by one middleware (`app/services/audit.py`) in the append-only
  `admin_audit_log` table (migration 0009). `GET /admin/audit` lists it.
- **Bulk and CSV:** `POST /admin/tests/bulk`, `PATCH /admin/sections/{id}/questions/bulk`,
  `GET /admin/sections/{id}/questions.csv`, `POST /admin/sections/{id}/questions/import-csv`
  (all-or-nothing, validated like the editor).
- **Analytics:** `GET /admin/tests/{id}/analytics` (per-question correct rate,
  skips, common wrong answers) and `GET /admin/analytics` (activity by module/week).
- **Limits and headers:** per-account rate limits on starting, saving and
  submitting attempts and on bulk admin calls (`app/services/rate_limit.py`);
  bodies over 2 MB get 413; security headers and `Cache-Control: no-store` on
  every API response; unexpected errors return a generic 500 with a correlation id.
- **Readiness:** `GET /health/ready` checks the database (used by the smoke test).
- **API docs:** `/docs` outside production; `python -m scripts.export_openapi`
  writes `docs/openapi.json`.

## Operations

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): environment audit, release order, CSP,
  rollback, backups/restore, quota monitoring, incidents.
- [`docs/ADMIN_GUIDE.md`](docs/ADMIN_GUIDE.md): writing and publishing content.
- `python -m scripts.smoke_test`: read-only check of the deployed apps.
- `python -m scripts.backup`: pg_dump + every test as JSON into `backups/` (gitignored).

## License

MIT. See [`LICENSE`](LICENSE). Contributions: [`CONTRIBUTING.md`](CONTRIBUTING.md).
