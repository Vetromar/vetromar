# Vetromar graph host server

The self-hostable graph host: keypair identities, invites, roles, and the
per-graph replication log that member devices replay. One server hosts any
number of shared graphs ("workspaces" in the schema). Server-side only —
the desktop sidecar's embedded host mode is the ONE place in `vetromar`
allowed to import this package (see CONTRIBUTING.md).

## Run locally / on a VPS

```sh
pip install -e ".[cloud]"
python -m cloud set-owner <your-public-key>   # from the app's Settings → Identity
python -m cloud --port 8787
```

Environment:

- `CLOUD_DATABASE_URL` — SQLAlchemy URL. Default `sqlite:///~/.vetromar/cloud-dev.db`.
  The schema is Postgres-portable; swap the URL at deploy time.
- `CLOUD_CORS_ORIGINS` — comma-separated allowed origins. Default `*` (dev).
- `CLOUD_PUBLIC_URL` — the base URL members reach this server at (baked into
  invite links by the app).
- `CLOUD_OWNER_PUBLIC_KEY` — enroll this key as owner at boot (container
  deploys; same effect as `set-owner`).

There are no accounts, no emails, no passwords. An identity is an Ed25519
public key; every proof is a signature over a server-issued single-use
nonce. The **owner** (the person the server belongs to) is the only
principal who can create graphs; everyone else enrolls through copyable
invite links generated in the desktop app.

## API sketch (`/v1`)

- `POST /auth/challenge` — `{public_key}` → single-use nonce (2-min TTL).
- `POST /auth/verify` — signed nonce → bearer token (enrolled keys only).
- `GET /me`, `GET /workspaces` — identity + memberships.
- `POST /workspaces` (owner) — create a graph; creator becomes `host`.
- Workspace-scoped routes require the `X-Workspace-Id` header:
  - `POST /invites` (host/admin; admin-role invites are host-only) — returns
    the raw invite token exactly once.
  - `POST /invites/accept` — invite token + public key + signed challenge +
    handle/display name → membership + session token.
  - `GET /members`, `DELETE /members/{principal_id}` (admins remove members,
    only the host removes admins, the host is irremovable; revokes the
    removed principal's tokens immediately).
  - `POST /members/{principal_id}/role` (host) — member ↔ admin.
  - `PUT /devices/{device_id}` — idempotent per-workspace device registration.
  - `POST /sync/push`, `GET /sync/pull?since=<seq>` — the replication log.
  - `DELETE /workspaces` (host) — proof-confirmed graph deletion.
- `DELETE /me` — proof-confirmed identity deletion (blocked while hosting a
  graph that still has members).

## Security notes / deliberate deferrals

- Identity: Ed25519. Session/invite tokens: 256-bit random, stored
  SHA-256-hashed, 30-day sliding / 14-day single-use. Challenges: single-use,
  2-minute TTL, stored hashed.
- Destructive routes re-require a freshly signed challenge — a stolen bearer
  token alone cannot destroy data.
- Rate limiting is a minimal in-memory per-IP counter on the unauthenticated
  routes. **At deploy time put a reverse proxy (real rate limits + TLS) in
  front** — this service assumes it terminates trusted transport.
- No alembic yet: `create_all` + additive `ensure_columns` only.
- Postgres concurrency: `push` locks the workspace row
  (`SELECT ... FOR UPDATE`, omitted by the SQLite dialect) before computing
  `MAX(seq)`, so concurrent pushes to one graph serialize.
- Log-only storage: the `changes` table is the graph's knowledge state;
  compaction/materialization are future work.
