# Self-hosting the Vetromar workspace server

The desktop app works with a **workspace server**: a small service that holds
accounts, invites, and the replication log that syncs your team's knowledge
across devices. You run it yourself — on your own machine, a home server, or
any cloud host. AI never routes through it; every AI call uses the provider
and keys you configure in the app.

## Quickstart (Docker Compose, Postgres)

```sh
git clone <this repo> && cd vetromar
docker compose up -d
```

Then in the desktop app's sign-in screen set the server to
`http://localhost:8787`, click **Create one on your server →**, and sign in
with the account you create.

## Single container (SQLite)

For a solo user or small team, Postgres is optional:

```sh
docker build -t vetromar-server .
docker run -d -p 8787:8787 \
  -v vetromar-data:/data \
  -e CLOUD_DATABASE_URL=sqlite:////data/cloud.db \
  -e CLOUD_PUBLIC_URL=http://localhost:8787 \
  vetromar-server
```

## Without Docker

```sh
pip install -e ".[cloud]"
python -m cloud --port 8787
```

Defaults to SQLite at `~/.vetromar/cloud-dev.db`.

## Railway (or any container host)

The repo's `Dockerfile` deploys directly on Railway/Fly/Render-class hosts:
create a service from the repo, add a Postgres add-on, and set
`CLOUD_DATABASE_URL` to its connection string (bare `postgresql://` URLs are
rewritten to the psycopg3 driver automatically). The server binds
`$PORT` when the platform injects one.

## Environment reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLOUD_DATABASE_URL` | `sqlite:///~/.vetromar/cloud-dev.db` | SQLAlchemy URL (SQLite or Postgres). |
| `CLOUD_PUBLIC_URL` | `http://localhost:8787` | The URL users reach this server at — used in password-reset links minted with `python -m cloud reset-link` (they point at the server's own `/reset-password` page). |
| `CLOUD_CORS_ORIGINS` | `*` | Comma-separated allowed browser origins. |

The server never sends email — the email address is just a login identifier.

## Account pages

The server serves its own account pages, same-origin with the API:

- `/signup` — create a workspace (first user becomes admin)
- `/invite-accept?token=…` — where invite links land
- `/reset-password?token=…` — where password-reset links land

## Invites and password resets (no email involved)

Invites are copyable links: an admin generates one in the desktop app's
Workspace tab and sends it over any channel (chat, however you like). It
works once and expires after 14 days.

Password resets work the same way — a workspace admin generates a one-time
reset link from the member list in the Workspace tab. If you run the server
and locked yourself out, mint one on the server box:

```sh
python -m cloud reset-link you@example.com
```

The link is single-use and expires in 60 minutes.

## Production notes

- **Put TLS + a reverse proxy in front** (Caddy/nginx/Traefik or your
  platform's ingress). The built-in per-IP rate limiting is minimal.
- Set `CLOUD_PUBLIC_URL` to your public `https://` URL, and
  `CLOUD_CORS_ORIGINS` away from `*`.
- **Back up the database.** The `changes` table IS your workspace's synced
  knowledge state (each member's device also keeps a full local replica, so
  a lost server can be re-seeded by any member's upload).
- Multi-device conflict handling is designed to converge; see
  `cloud/README.md` for the replication model and security notes.
