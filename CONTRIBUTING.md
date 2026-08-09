# Contributing to Vetromar

Thanks for helping! A few invariants keep this codebase trustworthy — please
read them before opening a PR.

## The hard invariants

1. **The evidence gate is the product.** Every knowledge unit stored must
   carry ≥1 evidence item validated against its episode's raw content,
   enforced inside `Store.add_unit` (and re-enforced when replicated changes
   are applied). Never weaken, bypass, or special-case it. Tolerance for
   imperfect models belongs BEFORE the gate (see `extraction/repair.py`,
   which snaps near-miss quotes to their literal span) — the stored evidence
   stays byte-literal.

2. **Frozen extraction surfaces.** These are pinned by a snapshot-hash test
   (`tests/test_extraction_shape.py`) and must not change without a
   deliberate, maintainer-approved decision:
   - the `ExtractedUnit` block in `vetromar/schema.py`
   - `vetromar/extraction/prompt.py`
   - the `ollama.chat(..., format=..., think=False)` call and
     `_UNIT_FIELD_ORDER` in `extraction/local_backend.py`
   - `validate_grounded_quotes` in `extraction/validate.py`

   They encode hard-won local-model tuning; casual edits regress extraction
   in ways tests don't catch.

3. **No per-source integration code, ever.** Vetromar talks to data sources
   only as a generic MCP client (spec-standard OAuth; `vetromar/sources/`).
   A new source is a catalog entry in `sources/catalog.py`, never an
   adapter. The generic ingestion surface is the only store door.

4. **Cheap-model tolerance.** Extraction and the sync agent must work on
   small/cheap models. Never fix a quality problem by requiring a bigger
   model — make the engine tolerant (healing, nudges, validation ladders).

5. **`cloud` never imports from `vetromar`** except
   `vetromar/workspace/wire.py` (the shared wire model), and `vetromar`
   never imports `cloud`. This keeps the desktop sidecar bundle free of
   server code and the server image free of ML dependencies.

6. **AI construction goes through `vetromar/ai.py`** (`get_provider`) —
   never instantiate provider SDK clients elsewhere. Gate AI features on
   `ai.ai_available(config)`, never on `config.api_key` directly.

7. **User-facing errors go through `errors.py:present_error`** in desktop
   surfaces: expected errors (ConfigError, workspace errors, rejected keys)
   pass through; everything else collapses to a generic message + VM-* code.

## Practical notes

- Run `python -m pytest tests/ -q` — the full suite must stay green.
  Tests for third-party SDK fakes should return the SDK's real object
  shapes (or use the wire-level fake servers in `tests/fixtures/`), not
  ad-hoc dicts.
- Store schema migrations are **additive only** (`store.py:_MIGRATIONS`);
  destructive migrations are not accepted.
- Config resolution is env > `~/.vetromar/config.toml` > default; secrets
  live in 0600 credential files, never in config.toml.
- Frontend: Svelte 5 runes; long-running work goes through the shared job
  trackers (`desktop/frontend/src/lib/jobs.svelte.js`) so it survives tab
  switches.
- UI-visible changes deserve a headless click-through against the real
  `vetromar ui-server` (see `.claude/skills/verify/SKILL.md` for the
  pattern).

## Scope principles

The moat is the fused, evidence-gated, bi-temporal graph. Features that a
generic meeting-summarizer could ship are table stakes — welcome, but they
must not compromise the invariants above. Browsing UI stays read-only
(episode-title rename is the one deliberate exception); corrections flow
through the CLI/MCP write surface.
