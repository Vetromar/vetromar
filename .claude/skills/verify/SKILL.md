---
name: verify
description: Build, launch, and drive the Vetromar desktop app / ui_server to verify changes at the real surface (headless Chrome click-through, isolated scratch env)
---

# Verifying Vetromar changes

## Launch the API + SPA (dev, unbundled)

```bash
cd desktop/frontend && npm run build          # ALWAYS rebuild dist first — the server serves it from disk
SCRATCH=$(mktemp -d)
printf 'backend = "api"\n' > "$SCRATCH/config.toml"
VETROMAR_CONFIG="$SCRATCH/config.toml" VETROMAR_DB="$SCRATCH/store.db" \
  nohup .venv/bin/vetromar ui-server > "$SCRATCH/server.log" 2>&1 &
# first line of server.log is PORT=<n>
```

- Scratch `VETROMAR_CONFIG` + `VETROMAR_DB` keep the developer's real config/store untouched.
- The REAL Anthropic key still resolves from `~/.vetromar/credentials` — so real-model live proofs work (Haiku, cents). The sources registry is NOT env-relocatable: `~/.vetromar/sources.toml` is shared. Back it up, add test sources, `vetromar sources remove <name>` after.
- `run_server` starts the auto-sync SCHEDULER (M13): with a scratch config it idles (auto_sync off by default), but if you enable auto-sync it WILL really sync every enabled source in the real registry — including any real connected sources.

## Deterministic sync source

Register the fake chat MCP server as a stdio source (3 tools, scripted content):

```bash
.venv/bin/vetromar connect fakechat --stdio "$PWD/.venv/bin/python $PWD/tests/fixtures/fake_mcp_server.py" --kind chat
```

A real Haiku sync of it takes ~15–30 s — a good window for testing in-flight UI states (tab switches, badges, cancel).

## Drive the UI

Playwright + installed Chrome works headless:

```python
p.chromium.launch(channel="chrome", headless=True)
page.goto(f"http://localhost:{PORT}/")
page.wait_for_selector("nav.tabs")   # app loaded
```

Useful selectors: `nav.tabs button:has-text('Sources')`, `.source-row`, `.sync-report`, `.activity-badge` (header badge for jobs on other tabs), `button:has-text('Settings')` → `Setup.svelte` (also the settings screen), `#autosync-interval`.

## Shared graphs (multi-instance)

`VETROMAR_HOME=<dir>` isolates a complete app instance (config, store,
graphs registry, identity key, host db) — N instances on one machine are N
"people". The canonical 3-person co-op check is scripted:

```bash
.venv/bin/python .claude/skills/verify/shared_graphs_e2e.py
# → "E2E: ALL CHECKS PASSED"; hosts on port 18795, cleans up after itself
```

It drives: host-mode on A → invites → B/C join → note + membrane share →
3-way convergence with @handle attribution → zero quarantine → MCP read of
the shared graph. Per-instance knobs: `VETROMAR_HOST_PORT` (embedded host),
`VETROMAR_GRAPH=<id>` (point the CLI/serve at a shared graph),
`VETROMAR_IDENTITY_KEY` (identity file). Stale listeners on the host port
shadow the embedded server on loopback — `lsof -ti :<port> | xargs kill`.

## Verify the bundled app (after desktop/build.sh)

- Build takes ~10 min; Tauri's dmg script fails headless — build.sh's hdiutil fallback handles it (exit 0 overall is what matters).
- The sidecar is an onedir bundle: the executable is `Vetromar.app/Contents/Resources/sidecar/vetromar-sidecar/vetromar-sidecar` (dir + inner binary, same name). Launch it directly with `ui-server` + scratch env and curl the routes — a stale sidecar 404s new routes (the M9b lesson).
- The frontend is embedded in the Tauri shell binary COMPRESSED — `strings` finds nothing. Instead check `desktop/frontend/dist/assets/index-*.js` (rebuilt by build.sh immediately before the shell compile) for your new markers.
- A running Vetromar.app instance keeps its old sidecar — quit + relaunch to pick up a rebuild.
