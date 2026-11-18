# prepband-backend

FastAPI backend for Prepband, per the project spec's Phase 1 ("foundation,
auth, protected API middleware"). **Not deployed anywhere yet** - Render
deployment is a separate, explicitly-requested step (see the project's
standing rule: no hosting MCP/skill usage unless asked).

## What's here

- `app/main.py` - app creation, CORS, exception handlers, router mounting.
- `app/core/config.py` - settings from env (`pydantic-settings`); Supabase
  project URL/JWKS URL default to this project's real (non-secret) values so
  `/health` and JWT verification work with zero `.env` setup in local dev.
- `app/core/security.py` - `get_current_user_id`: verifies a Supabase access
  token's signature against the project's live JWKS endpoint (confirmed this
  project signs asymmetrically with ES256, not the legacy HS256 shared
  secret), plus issuer/audience/expiry. Identity always comes from the
  verified token's `sub` claim, never a client-supplied id (FR-AUTH-07).
- `app/core/roles.py` - `require_admin`: a FastAPI dependency gating
  admin-only routes on the new `user_roles` table. Admin can't be
  self-assigned from the client - see the migration below.
- `app/core/errors.py` - consistent `{"error": {code, message,
  correlation_id, retryable}}` JSON envelope for `HTTPException` and
  validation errors, per spec section 8.1.
- `app/db/session.py` - SQLModel engine/session from `DATABASE_URL`. Returns
  a clear `503` instead of crashing if that env var isn't set.
- `app/models.py` - SQLModel mappings for the existing `profiles` and
  `user_roles` tables. **Schema is owned by Supabase migrations** (applied
  via the Supabase MCP, mirrored in
  `prepband-frontend/src/lib/database.types.ts`) - never run
  `SQLModel.metadata.create_all()` against these.
- `app/api/v1/`: `GET /health` (public), `GET /me` + `PATCH /me`
  (authenticated profile read/update), `GET /admin/ping` (authenticated +
  admin-only, a minimal proof that the role gate works - not the real admin
  CMS, which isn't built yet).

## New in this pass: `user_roles`

Migration `add_user_roles_table` added `public.user_roles` (`user_id`,
`role` defaulting to `'student'`, checked against
`'student' | 'admin' | 'super_admin'`). A `handle_new_user_role` trigger
(locked down the same way as `handle_new_user`: `SECURITY DEFINER`, `EXECUTE`
revoked from `anon`/`authenticated`) inserts a `student` row on signup. RLS
only grants `select` on your own row - there is deliberately no
insert/update/delete policy, so a role can only ever be changed by someone
running SQL directly (e.g. via the Supabase MCP or dashboard), never through
the API or frontend. To make yourself an admin for local testing:

```sql
update public.user_roles set role = 'admin' where user_id = '<your auth.users id>';
```

## What this backend deliberately does *not* do

- **No avatar/R2 upload endpoint.** An earlier prototype here signed
  presigned R2 PUT URLs, but nothing ever called it, and Prepband's real
  avatar upload now goes through a Supabase Edge Function
  (`upload-avatar`) that talks to R2 directly - see CLAUDE.md's "Avatar
  storage (Cloudflare R2)" section. That prototype (`app/r2.py` and its
  endpoint) has been removed; this backend has no R2 credentials and
  shouldn't gain any.
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
need `DATABASE_URL` (Supabase dashboard -> Project Settings -> Database ->
Connection string) filled into `.env` first; until then they return a clear
`503 "Database is not configured yet."` rather than a stack trace.

`CORS_ALLOWED_ORIGINS` must include the frontend's dev origin
(`http://localhost:5180`, already the default) for the frontend to call this
API from the browser once it's wired up.
