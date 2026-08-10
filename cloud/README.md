# Vetromar workspace server

The self-hostable workspace service: accounts, invites, and the per-workspace
replication log that multi-device sync replays. Server-side only — the desktop
sidecar never imports this package.

## Run locally

```sh
pip install -e ".[cloud]"
python -m cloud --port 8787
```

Environment:

- `CLOUD_DATABASE_URL` — SQLAlchemy URL. Default `sqlite:///~/.vetromar/cloud-dev.db`.
  The schema is Postgres-portable; swap the URL at deploy time.
- `CLOUD_CORS_ORIGINS` — comma-separated allowed origins. Default `*` (dev).
- `CLOUD_PUBLIC_URL` — the base URL users reach this server at; used in
  password-reset links minted by `python -m cloud reset-link`. (Legacy name
  `CLOUD_WEBSITE_URL` is honored.)

The server never sends email — the email address is a login identifier only.
Invites are copyable links generated in the desktop app; password resets are
one-time links minted by a workspace admin in the app, or by the operator on
the server box:

```sh
python -m cloud reset-link you@example.com
```

## API sketch (`/v1`)

- `POST /workspaces` — signup: workspace + first admin → token.
- `POST /auth/login` — email + password → token (desktop app).
- `GET /me` — user, workspace, role.
- `POST /invites` (admin) — returns the raw invite token exactly once.
- `POST /invites/accept` — invite token + name/email/password → member account.
- `GET /members`, `DELETE /members/{user_id}` (admin; revokes the removed
  user's tokens immediately; last-admin guarded).
- `POST /members/{user_id}/reset-link` (admin) — mint a one-time
  password-reset link; `POST /auth/reset-confirm` consumes it.
- `PUT /devices/{device_id}` — idempotent device registration.
- `POST /sync/push`, `GET /sync/pull?since=<seq>` — the replication log.
- `DELETE /workspaces` (admin) / `DELETE /me` — password-confirmed deletion.

## Security notes / deliberate deferrals

- Passwords: argon2id (argon2-cffi defaults). Tokens and invite tokens:
  256-bit random, stored SHA-256-hashed, 30-day sliding / 14-day single-use.
- Rate limiting is a minimal in-memory per-IP counter on the credential
  routes. **At deploy time put a reverse proxy (real rate limits + TLS) in
  front** — this service assumes it terminates trusted transport.
- No alembic yet: `create_all` only.
- Postgres concurrency: `push` locks the workspace row
  (`SELECT ... FOR UPDATE`, omitted by the SQLite dialect) before computing
  `MAX(seq)`, so concurrent pushes to one workspace serialize.
- Log-only storage: the `changes` table is the workspace knowledge state;
  compaction/materialization are future work.
