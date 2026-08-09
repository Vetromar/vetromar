# Vetromar desktop app

A macOS desktop UI for the capture engine: **import an audio file**, **record
live**, and **choose/provision the extraction backend** — no terminal needed.
It's a thin shell over the existing engine, not a reimplementation.

## Architecture

```
Tauri shell (Rust + macOS WebView)          desktop/src-tauri/
  │  spawns, reads PORT=<n> from stdout, injects window.__VETROMAR_API__
  ▼
Python sidecar — FastAPI on 127.0.0.1:<port>   vetromar/ui_server/
  │  calls the same entry points the CLI calls
  ▼
Engine: run_pipeline / record_mic / operations / Store   (unchanged)
```

- **Frontend** (`frontend/`): Svelte + Vite SPA → static `dist/`. Talks only to
  the local API via `src/api.js`.
- **Sidecar** (`vetromar ui-server`): the engine behind an HTTP API. Long jobs
  (transcribe + extract) run on background threads; the UI polls `/api/jobs/{id}`.
- **Shell** (`src-tauri/`): launches the sidecar, wires the port into the
  webview, kills the sidecar on exit. Mic permission via `Info.plist`.

## Develop

```bash
# 1. Engine (once): from the repo root
pip install -e ".[capture,ui]"

# 2. Frontend deps (once)
cd desktop/frontend && npm install && cd ..

# 3. Run the app in dev. Point the shell at the venv's engine:
cd desktop
VETROMAR_SIDECAR="$PWD/../.venv/bin/vetromar" npm run dev
```

`npm run dev` starts Vite (port 5173) and `tauri dev`, which spawns the sidecar
and opens the window. Without `VETROMAR_SIDECAR`, the shell falls back to a
`vetromar` on PATH.

To iterate on the API alone (no desktop shell):

```bash
vetromar ui-server --port 8765      # prints PORT=8765, serves the API
curl localhost:8765/api/health
```

## Build the installable app (macOS)

```bash
cd desktop
./build.sh                 # sidecar (PyInstaller) + frontend + Tauri → .app/.dmg
./build.sh --sidecar       # just rebuild/smoke-test the Python sidecar
```

Output: `src-tauri/target/release/bundle/{macos,dmg}/` — `Vetromar.app` (~1.2 GB)
and `Vetromar_0.1.0_aarch64.dmg` (~516 MB). The bundle embeds the PyInstaller
`onedir` engine under `Resources/sidecar/` (multi-GB, since it carries
torch/whisperx/pyannote). Tauri's `.dmg` step drives Finder and fails in a
headless session; `build.sh` falls back to `hdiutil` (no Finder needed) so the
`.dmg` is still produced. The `.app` runs directly regardless.

**Model weights** are *not* in the bundle. On first run, `Local` setup provisions
the Ollama runtime + model (existing `runtime/` path); Whisper/pyannote weights
download on first capture. This keeps the artifact to the code+deps.

**Signing:** the build is unsigned. On your own Mac, right-click the app →
**Open** the first time (Gatekeeper). Distributing to other machines needs an
Apple Developer ID + notarization.

## Files

| Path | What |
|---|---|
| `frontend/src/App.svelte` | shell + health gate (Setup vs Capture) |
| `frontend/src/lib/*.svelte` | Setup / Capture / Results screens |
| `frontend/src/api.js` | the only place that knows the API shape |
| `src-tauri/src/main.rs` | sidecar spawn + port handshake + cleanup |
| `vetromar-sidecar.spec` | PyInstaller bundle definition |
| `build.sh` | one-command build |
