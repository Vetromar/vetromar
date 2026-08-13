# Self-hosting a Vetromar graph host

A **graph host** is a small service that holds shared graphs: keypair
identities, invites, and the replication log that syncs each graph across
its members' devices. The easiest way to host is inside the desktop app on
an always-on machine (Host mode); this guide covers the headless
alternative — a VPS or any box you run yourself. AI never routes through
the server; every AI call uses the provider and keys each member configures
in their own app.

There are **no accounts and no passwords**: your identity is a key generated
by the app (Settings → Identity shows its public half). The server's
**owner** — the one key allowed to create graphs — is enrolled with
`set-owner`. Members join through invite links and manage everything from
the app.

## Quickstart (Docker Compose, Postgres)

```sh
git clone <this repo> && cd vetromar
docker compose up -d
docker compose exec vetromar-server python -m cloud set-owner <your-public-key>
```

Then create graphs and mint invites from the desktop app, pointing it at
`http://your-host:8787`.

## Single container (SQLite)

For one person's graphs, Postgres is optional:

```sh
docker build -t vetromar-server .
docker run -d -p 8787:8787 \
  -v vetromar-data:/data \
  -e CLOUD_DATABASE_URL=sqlite:////data/cloud.db \
  -e CLOUD_PUBLIC_URL=http://localhost:8787 \
  -e CLOUD_OWNER_PUBLIC_KEY=<your-public-key> \
  vetromar-server
```

## Without Docker

```sh
pip install -e ".[cloud]"
python -m cloud set-owner <your-public-key>
python -m cloud --port 8787
```

Defaults to SQLite at `~/.vetromar/cloud-dev.db`.

## Railway (or any container host)

The repo's `Dockerfile` deploys directly on Railway/Fly/Render-class hosts:
create a service from the repo, add a Postgres add-on, and set
`CLOUD_DATABASE_URL` to its connection string (bare `postgresql://` URLs are
rewritten to the psycopg3 driver automatically). Set
`CLOUD_OWNER_PUBLIC_KEY` to enroll yourself at boot. The server binds
`$PORT` when the platform injects one.

## Reachability (home hosting)

Members' apps must be able to reach the server. Three transports work; the
app treats the address as opaque:

- **Mesh VPN (Tailscale etc.)** — the zero-port-forwarding path, and the
  only one that works behind carrier-grade NAT (apartment-building wifi).
  Put the host machine on a tailnet, invite your members to it, and use the
  tailnet address in invite links.
- **Port forwarding** — the classic Minecraft-server move: forward a port
  on your router to the host machine, share your public address (a dynamic
  DNS name survives IP changes).
- **A VPS** — rent a small box; it's publicly reachable by construction.

## Environment reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLOUD_DATABASE_URL` | `sqlite:///~/.vetromar/cloud-dev.db` | SQLAlchemy URL (SQLite or Postgres). |
| `CLOUD_PUBLIC_URL` | `http://localhost:8787` | The URL members reach this server at — the app bakes it into invite links. |
| `CLOUD_CORS_ORIGINS` | `*` | Comma-separated allowed browser origins. |
| `CLOUD_OWNER_PUBLIC_KEY` | — | Enroll this key as the server owner at boot (same as `set-owner`). |

## Invites (no email involved)

Invites are copyable links: the host (or an admin) generates one in the
desktop app and sends it over any channel. It works once and expires after
14 days. Opening one in a browser shows instructions; the actual join
happens in the app (paste the link), because enrollment signs a challenge
with the member's local key. Locked yourself out? You can't be — possession
of your key file IS your access; there is nothing to reset. (Back up
`~/.vetromar/identity.key`.)

## Production notes

- **Put TLS + a reverse proxy in front** (Caddy/nginx/Traefik or your
  platform's ingress) for anything on the open internet. The built-in
  per-IP rate limiting is minimal.
- Set `CLOUD_PUBLIC_URL` to your public URL, and `CLOUD_CORS_ORIGINS` away
  from `*`.
- **Back up the database.** The `changes` table IS each graph's synced
  knowledge state (each member's device also keeps a full local replica, so
  a lost server can be re-seeded by any member's upload).
- Multi-device conflict handling is designed to converge; see
  `cloud/README.md` for the replication model and security notes.
