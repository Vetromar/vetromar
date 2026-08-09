# Vetromar — orientation for AI-assisted development

Local-first knowledge engine (AGPL-3.0): captures meetings + syncs sources
into one evidence-gated, bi-temporal graph, queryable over MCP. Read
`CONTRIBUTING.md` first — its invariants are non-negotiable and are
summarized here for quick reference.

## Layout

- `vetromar/` — the engine + CLI + desktop sidecar package.
  - `schema.py` universal shapes (`Unit`/`Episode`/`Entity`/`Edge`);
    `store/` graph-shaped SQLite (FTS5 + vectors, additive migrations only);
    `extraction/` (frozen meeting path + generic extraction + `repair.py`
    quote healing); `search/` hybrid FTS+embedding retrieval; `linking/`
    auto-linking; `capture/` audio pipeline; `transcription/` local-vs-
    Deepgram seam; `sources/` generic MCP client (catalog/OAuth/sync agent);
    `providers/` BYO AI providers (anthropic + openai-compat);
    `mcp_server/` the read/write MCP surface (`vetromar serve`);
    `workspace/` self-hosted sync client; `ui_server/` FastAPI behind the
    desktop app; `operations.py` shared CLI/UI logic; `views.py` shared
    read views; `ai.py` the ONE provider chooser.
- `cloud/` — the self-hostable workspace server (accounts/invites/sync).
  Never imports `vetromar` except `workspace/wire.py`; never imported by
  `vetromar`.
- `desktop/` — Svelte 5 frontend + Tauri shell + PyInstaller sidecar spec.
  Full build: `desktop/build.sh`. Frontend-only changes can rebuild
  shell-only, but ANY Python delta needs the full build (a stale sidecar
  404s new routes).
- `website/` — static project site (Vercel). `docs/` — self-hosting guide.

## Build & test

```sh
pip install -e ".[dev]"           # or +capture, +ui, +cloud
python -m pytest tests/ -q        # full suite must stay green
cd desktop/frontend && npm run build
python -m cloud --port 8787       # self-hosted workspace server, dev
```

## Hard rules (see CONTRIBUTING.md for the full versions)

- The evidence gate inside `Store.add_unit` is the product — never weaken
  it; model tolerance goes BEFORE the gate (`extraction/repair.py`).
- Frozen surfaces (snapshot-hash-pinned in `tests/test_extraction_shape.py`):
  the `ExtractedUnit` block, `extraction/prompt.py`, the
  `ollama.chat(..., format=..., think=False)` call + `_UNIT_FIELD_ORDER`,
  `validate_grounded_quotes`.
- No per-source integration code — sources are catalog entries; MCP is the
  sole adapter in both directions.
- Cheap-model constraint: never fix quality by requiring a bigger model.
- AI clients only via `ai.get_provider`; gate on `ai.ai_available(config)`.
- Desktop-visible errors via `errors.py:present_error`.
- Config: env > `~/.vetromar/config.toml` > default; secrets in 0600
  credential files only.
- Store migrations additive only (`store.py:_MIGRATIONS`).

## Gotchas worth knowing

- Anthropic haiku-tier models 400 on the adaptive-thinking param — the gate
  in `providers/anthropic.py` handles it; keep it.
- The OpenAI-compat provider negotiates structured output in three tiers
  (strict json_schema → json_object → prompted JSON) and caches the result
  per instance; wire-level tests live against
  `tests/fixtures/fake_openai_server.py`.
- SQLite is thread-bound: jobs/routes open their own `Store`.
- Svelte 5: module-scope rune trackers in `lib/jobs.svelte.js` keep jobs
  alive across tab switches; wheel handlers need `{passive:false}`.
- Kill scratch dev servers by PID (`lsof -ti :PORT | xargs kill`) — `kill
  %N` across shell invocations is a no-op.
- Headless verification pattern for UI changes:
  `.claude/skills/verify/SKILL.md`.
