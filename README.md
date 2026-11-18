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
- **No test-engine endpoints yet** (attempts, responses, grading,
  certificates). That's Phase 2+ per the spec's development plan.

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

## Deployment

Deployed on Render (free-tier web service, Frankfurt region):
**https://prepband-backend.onrender.com**. Continuous deployment is
connected to this repo's `main` branch (`PrepBand/Prepband-backend` on
GitHub) — a push to `main` triggers a build and redeploy automatically.
Build: `pip install -r requirements.txt`. Start:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`. `PYTHON_VERSION` is
pinned via a Render env var (currently `3.12.7`) rather than riding
Render's own default, since a prior default bump (to Python 3.14) broke
the build — `psycopg[binary]` had no wheel for it yet.

## Before touching any external service

The user works on other, unrelated projects through the same connected
MCP/plugin accounts (GitHub, Render, Cloudflare, Supabase, Resend, Netlify,
etc.). **Before any write/mutating call through one of those services:
confirm the resource is actually named for Prepband, then get the user's
explicit approval for that specific action** — there are no standing
exceptions for any of these services. See `../CLAUDE.md`'s "Cross-project
safety rule" and `../rules.md` for the full rule. If you can't confirm the
resource is Prepband's own, make no change and ask instead of guessing.
