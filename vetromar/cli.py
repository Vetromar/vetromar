"""Vetromar CLI — the developer-facing surface.

Setup:              setup, doctor
Sources:            connect, sources (list/remove/test), sync
Capture:            capture, record, import-transcript
Concierge entry:    add-episode, add-unit, new-entity, link-alias,
                    link-units, supersede
Views:              render, episodes, units, show
Read surface:       serve (MCP)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from vetromar import operations
from vetromar.config import load_config
from vetromar.errors import ConfigError
from vetromar.store import Store

app = typer.Typer(help="Vetromar: decision capture + store + MCP read surface.")


def _open_store() -> Store:
    config = load_config()
    config.ensure_dirs()
    return Store(config.db_path)


def _parse_when(when: Optional[str]) -> datetime:
    if when is None:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(when)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ensure_local_backend_ready(config) -> None:
    """Guarantee the managed Ollama server before extraction (local mode).

    Shared with the desktop UI API — see `operations.ensure_backend_ready`."""
    operations.ensure_backend_ready(config)


# -- setup & health -----------------------------------------------------------

_RAM_TIERS = """Local model RAM tiers (measured; see CLAUDE.md):
  <12 GB  no viable local tier — choose Cloud instead
  16–24 GB  qwen3.5:9b (default) — ~30 s–2 min per meeting
  ≥32 GB  qwen3.5:27b — overnight tier on less RAM
"""


def _make_progress():
    """A carriage-return progress reporter for the setup provisioning steps."""
    state = {"last": 0}

    def report(label: str, completed: "int | None", total: "int | None") -> None:
        if total:
            line = f"  {label}: {int(completed / total * 100)}%"
        else:
            line = f"  {label}…"
        sys.stdout.write("\r" + line.ljust(state["last"]))
        sys.stdout.flush()
        state["last"] = len(line)

    return report


def _setup_cloud() -> None:
    """Cloud tier: the user's own AI provider — an Anthropic key here, or any
    OpenAI-compatible endpoint (configure via env/config.toml or the app)."""
    from vetromar.ai import ai_available

    config = load_config()
    if ai_available(config):
        operations.select_api_backend(config)
        detail = (
            f"OpenAI-compatible endpoint {config.openai_base_url}"
            if config.ai_provider == "openai"
            else "Anthropic key"
        )
        typer.secho(f"✓ Cloud mode ready ({detail}).", fg=typer.colors.GREEN)
        return
    if typer.confirm("Paste an Anthropic API key?", default=True):
        key = typer.prompt("Paste your Anthropic API key", hide_input=True).strip()
        typer.echo("Validating key…")
        try:
            operations.validate_and_save_api_key(key)
        except operations.InvalidApiKey as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        typer.secho("✓ Cloud mode ready.", fg=typer.colors.GREEN)
        return
    typer.echo(
        "To use any other provider, set VETROMAR_AI_PROVIDER=openai, "
        "VETROMAR_OPENAI_BASE_URL (e.g. https://api.openai.com/v1 or "
        "http://localhost:11434/v1 for Ollama), OPENAI_API_KEY if needed, and "
        "VETROMAR_API_MODEL — or configure it in the desktop app's Settings."
    )
    raise typer.Exit(1)


def _setup_transcription() -> None:
    """Optional fast-tier key: cloud transcription via the user's own
    Deepgram key. Without one, transcription runs locally."""
    typer.echo("")
    if not typer.confirm(
        "Enable fast cloud transcription via a developer Deepgram key? "
        "(optional — audio is uploaded to Deepgram)",
        default=False,
    ):
        typer.echo(
            "Transcription stays local (private, slower). Re-run `vetromar setup` "
            "to enable the fast tier later."
        )
        return
    key = typer.prompt("Paste your Deepgram API key", hide_input=True).strip()
    typer.echo("Validating key…")
    try:
        operations.validate_and_save_deepgram_key(key)
    except operations.InvalidApiKey as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho("✓ Cloud transcription enabled.", fg=typer.colors.GREEN)


def _setup_local() -> None:
    config = load_config()
    config.ensure_dirs()
    typer.echo(_RAM_TIERS)
    if not typer.confirm(
        f"Provision local model {config.local_model} (no Ollama install needed)?", default=True
    ):
        raise typer.Exit(0)
    operations.provision_local(config, progress=_make_progress())
    sys.stdout.write("\n")
    typer.secho("✓ Local mode ready — data never leaves this machine.", fg=typer.colors.GREEN)


@app.command()
def setup():
    """First-run: choose Cloud or Local extraction, and provision it end-to-end."""
    typer.echo("Vetromar setup — how should meetings be extracted?\n")
    typer.echo("  [1] Cloud  — your own AI provider: Anthropic key or any OpenAI-compatible endpoint")
    typer.echo("  [2] Local  — private, on-device (downloads a model, nothing leaves)\n")
    choice = typer.prompt("Choose 1 or 2", default="1").strip().lower()
    if choice in ("2", "local"):
        _setup_local()
    else:
        _setup_cloud()
    _setup_transcription()
    typer.echo('\nNext: vetromar capture <audio> --title "Meeting title"')


def _status_line(ok: bool, label: str, detail: str = "") -> None:
    mark = typer.style("✓", fg=typer.colors.GREEN) if ok else typer.style("✗", fg=typer.colors.RED)
    typer.echo(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))


@app.command()
def doctor(
    check_api: bool = typer.Option(False, "--check-api", help="Also make a live API auth call"),
):
    """Health/preflight report. Exits non-zero if the selected backend isn't ready."""
    config = load_config()
    report = operations.health_report(config, check_api=check_api)
    typer.echo(f"Vetromar doctor — backend: {report['backend']}\n")

    for check in report["checks"]:
        _status_line(check["ok"], check["label"], check["detail"])
    ready = report["ready"]

    # Informational — whisperx is only needed for LOCAL transcription; with the
    # cloud tier active its absence is fine (torch-free install), not a fault.
    try:
        import whisperx  # noqa: F401

        _status_line(True, "capture extra (whisperx) importable")
    except Exception:  # noqa: BLE001
        if report.get("transcription") == "cloud":
            _status_line(
                True,
                "capture extra (whisperx) not installed",
                "not needed — cloud transcription active; required for VETROMAR_TRANSCRIBE=local",
            )
        else:
            _status_line(False, "capture extra not installed", 'pip install -e ".[capture]"')

    # Informational — only the `record` mic path needs sounddevice/soundfile.
    try:
        import sounddevice  # noqa: F401
        import soundfile  # noqa: F401

        _status_line(True, "mic capture extra (sounddevice) importable")
    except Exception:  # noqa: BLE001
        _status_line(False, "mic capture extra not installed", 'pip install -e ".[capture]"')

    # Informational — connected MCP sources (config + token presence, no network).
    from vetromar.sources.auth import tokens_dir
    from vetromar.sources.registry import load_sources

    for source in load_sources():
        if source.url:
            has_tokens = (tokens_dir() / f"{source.name}.json").exists()
            _status_line(
                has_tokens,
                f"source '{source.name}' authorized",
                "" if has_tokens else f"run `vetromar connect {source.name}`",
            )
        else:
            _status_line(True, f"source '{source.name}' (local command)")

    typer.echo("")
    if ready:
        typer.secho("Ready.", fg=typer.colors.GREEN)
    else:
        typer.secho("Not ready — run `vetromar setup`.", fg=typer.colors.YELLOW)
    raise typer.Exit(0 if ready else 1)


# -- capture ----------------------------------------------------------------


@app.command()
def capture(
    audio: Path = typer.Argument(..., help="Recording to import (wav/m4a/mp3/...)"),
    title: Optional[str] = typer.Option(
        None, "--title", "-t", help="Meeting title (default: date & time)"
    ),
    when: Optional[str] = typer.Option(None, help="Meeting datetime, ISO 8601 (default: now)"),
):
    """Full pipeline: audio -> diarized transcript -> units -> store -> markdown."""
    from vetromar.capture.pipeline import run_pipeline

    config = load_config()
    config.ensure_dirs()
    _ensure_local_backend_ready(config)
    store = _open_store()
    occurred_at = _parse_when(when)
    episode, units, markdown = run_pipeline(
        audio,
        title=title or operations.default_meeting_title(occurred_at),
        config=config,
        store=store,
        occurred_at=occurred_at,
    )
    typer.echo(f"episode {episode.id}: {len(units)} unit(s) written to store\n")
    typer.echo(markdown)


@app.command()
def record(
    title: Optional[str] = typer.Option(
        None, "--title", "-t", help="Meeting title (default: date & time)"
    ),
    duration: Optional[float] = typer.Option(
        None, "--duration", "-d", help="Seconds to record (default: until Ctrl-C)"
    ),
    keep: Optional[Path] = typer.Option(
        None, "--keep", help="Where to save the recording (default: <store>/recordings/)"
    ),
    when: Optional[str] = typer.Option(None, help="Meeting datetime, ISO 8601 (default: now)"),
):
    """Record from the machine mic, then run the full capture pipeline on it."""
    from vetromar.capture.audio import record_mic
    from vetromar.capture.pipeline import run_pipeline

    config = load_config()
    config.ensure_dirs()
    _ensure_local_backend_ready(config)
    store = _open_store()

    out_path = keep or (
        config.db_path.parent
        / "recordings"
        / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.wav"
    )
    typer.echo("Recording…" + ("" if duration else " press Ctrl-C to stop."))
    audio = record_mic(out_path, duration_s=duration)
    typer.echo(f"saved recording → {audio}")

    occurred_at = _parse_when(when)
    episode, units, markdown = run_pipeline(
        audio,
        title=title or operations.default_meeting_title(occurred_at),
        config=config,
        store=store,
        occurred_at=occurred_at,
    )
    typer.echo(f"episode {episode.id}: {len(units)} unit(s) written to store\n")
    typer.echo(markdown)


@app.command("import-transcript")
def import_transcript(
    transcript: Path = typer.Argument(..., help="Transcript JSON (pipeline output or fixture)"),
    title: str = typer.Option(..., "--title", "-t"),
    when: Optional[str] = typer.Option(None, help="Meeting datetime, ISO 8601"),
):
    """Stages 4-6 only: extract from an existing transcript and store the units."""
    from vetromar.capture.pipeline import load_transcript_file, run_from_transcript

    config = load_config()
    config.ensure_dirs()
    _ensure_local_backend_ready(config)
    store = _open_store()
    t = load_transcript_file(transcript)
    episode, units, markdown = run_from_transcript(
        t, title=title, config=config, store=store,
        occurred_at=_parse_when(when), raw_ref=str(transcript),
    )
    typer.echo(f"episode {episode.id}: {len(units)} unit(s) written to store\n")
    typer.echo(markdown)


@app.command()
def ingest(
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Text file with the source's raw content"
    ),
    title: str = typer.Option(..., "--title", "-t"),
    kind: str = typer.Option("document", "--kind", help="email_thread / chat / document / note / ..."),
    when: Optional[str] = typer.Option(None, help="When the source event happened, ISO 8601"),
    extract: bool = typer.Option(
        True, "--extract/--no-extract",
        help="Run generic extraction over the raw text (api backend only)",
    ),
    external_id: Optional[str] = typer.Option(
        None, "--external-id",
        help="The content's stable id at its source (dedup key, unique in the store)",
    ),
):
    """Generic ingestion: land ANY text source as a raw episode and extract
    typed, excerpt-evidenced units from it."""
    from vetromar.extraction.generic import extract_from_raw
    from vetromar.ingest.generic import ingest_episode

    config = load_config()
    config.ensure_dirs()
    store = _open_store()
    ep = ingest_episode(
        store,
        title=title,
        source_kind=kind,
        raw=source.read_text(),
        occurred_at=_parse_when(when),
        external_id=external_id,
    )
    typer.echo(f"created {ep.source_kind} episode {ep.id}")
    if extract:
        units = extract_from_raw(store, ep, config)
        typer.echo(f"{len(units)} unit(s) extracted and stored")
        for u in units:
            typer.echo(f"  {u.id}  [{u.type}]  {u.content}")


@app.command("ingest-file")
def ingest_file(
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Document file (.pdf/.docx/.md/.txt)"
    ),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Defaults to the filename"),
    when: Optional[str] = typer.Option(None, help="When the document dates from, ISO 8601"),
):
    """Ingest a document file: parse it locally, land it as a `document`
    episode, and extract typed units whose evidence carries page/paragraph
    locators."""
    from vetromar import operations

    config = load_config()
    config.ensure_dirs()
    store = _open_store()
    ep, units = operations.ingest_document(
        store, config, source, title=title, occurred_at=_parse_when(when)
    )
    typer.echo(f"created document episode {ep.id} ({ep.title})")
    typer.echo(f"{len(units)} unit(s) extracted and stored")
    for u in units:
        locator = next(
            (ev.locator for ev in u.evidence if getattr(ev, "locator", None)), None
        )
        suffix = f"  ({locator})" if locator else ""
        typer.echo(f"  {u.id}  [{u.type}]  {u.content}{suffix}")


@app.command("dedupe-entities")
def dedupe_entities_cmd():
    """Merge duplicate entities across the store (same person under several
    refs). Redirect-based: aliases union into the older entity, reads follow
    the redirect, edges and history stay put."""
    from vetromar.linking.dedupe import dedupe_entities

    config = load_config()
    config.ensure_dirs()
    store = _open_store()
    report = dedupe_entities(store, config)
    typer.echo(
        f"{report.merged} entit{'y' if report.merged == 1 else 'ies'} merged"
        + (f" ({report.llm_pairs_judged} pair(s) judged)" if report.llm_pairs_judged else "")
    )
    for err in report.errors:
        typer.secho(f"  {err}", fg=typer.colors.RED)


# -- sources (Vetromar as MCP client) ----------------------------------------

sources_app = typer.Typer(help="Connected MCP sources (the customer's stack).")
app.add_typer(sources_app, name="sources")


def _connect_one(
    name: str,
    url: str | None,
    command: str | None,
    kind: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> None:
    from vetromar.sources import auth as sources_auth
    from vetromar.sources import client as sources_client
    from vetromar.sources.catalog import catalog_entry
    from vetromar.sources.registry import SourceConfig, upsert_source

    if client_id:
        sources_auth.seed_client_info(name, client_id, client_secret)
    entry = catalog_entry(name)
    if (
        entry is not None
        and entry.needs_client_registration
        and not sources_auth.has_client_info(name)
    ):
        raise ConfigError(
            f"{name} requires a pre-registered OAuth app — its server does not "
            "support automatic client registration.",
            hint=f"Create an app at {entry.setup_url} with redirect URL "
            f"{sources_auth.relay_redirect_uri()}, then re-run "
            f"`vetromar connect {name} --client-id … --client-secret …`.",
        )
    if command:
        parts = command.split()
        source = SourceConfig(name=name, command=parts[0], args=parts[1:], source_kind=kind)
    else:
        source = SourceConfig(name=name, url=url, source_kind=kind)
    typer.echo(f"Connecting to {name}" + (f" ({url})" if url else " (local command)") + " …")
    tools = sources_client.test_source(source)  # remote: runs the browser consent
    upsert_source(source)
    typer.secho(f"✓ {name} connected — {len(tools)} tool(s) available", fg=typer.colors.GREEN)


@app.command()
def connect(
    name: Optional[str] = typer.Argument(None, help="Catalog name (see `vetromar connect` with no args)"),
    url: Optional[str] = typer.Option(None, "--url", help="Custom remote MCP server URL"),
    stdio: Optional[str] = typer.Option(None, "--stdio", help="Custom local MCP server command, quoted"),
    kind: str = typer.Option("document", "--kind", help="source_kind for episodes from this source"),
    client_id: Optional[str] = typer.Option(
        None, "--client-id", help="OAuth client ID for providers without automatic registration"
    ),
    client_secret: Optional[str] = typer.Option(
        None, "--client-secret", help="OAuth client secret to go with --client-id"
    ),
):
    """Connect Vetromar to your stack: pick sources, click Allow in the
    browser — no tokens, no config files."""
    from vetromar.sources.catalog import CATALOG, catalog_entry

    if url or stdio:
        if not name:
            raise ConfigError("A custom source needs a name: `vetromar connect <name> --url/--stdio …`")
        _connect_one(name, url, stdio, kind, client_id, client_secret)
        return
    if name:
        entry = catalog_entry(name)
        if entry is None:
            raise ConfigError(
                f"'{name}' is not in the catalog.",
                hint="Run `vetromar connect` with no arguments to see it, or pass --url/--stdio for a custom server.",
            )
        _connect_one(entry.name, entry.url, None, entry.source_kind, client_id, client_secret)
        return
    typer.echo("Connect your stack — each choice is one browser consent click:\n")
    for i, entry in enumerate(CATALOG, 1):
        typer.echo(f"  [{i}] {entry.name:<10} {entry.description}")
    typer.echo("")
    raw_choice = typer.prompt("Which sources? (numbers comma-separated, or 'all')").strip().lower()
    if raw_choice == "all":
        chosen = list(CATALOG)
    else:
        try:
            chosen = [CATALOG[int(c) - 1] for c in raw_choice.replace(" ", "").split(",") if c]
        except (ValueError, IndexError):
            raise ConfigError(f"Could not read the selection {raw_choice!r}.")
    for entry in chosen:
        _connect_one(entry.name, entry.url, None, entry.source_kind)


@sources_app.command("list")
def sources_list():
    """Connected sources and their transports."""
    from vetromar.sources.registry import load_sources

    sources = load_sources()
    if not sources:
        typer.echo("No sources connected. Run `vetromar connect`.")
        return
    for s in sources:
        where = s.url or f"{s.command} {' '.join(s.args)}".strip()
        flag = "" if s.enabled else "  [disabled]"
        typer.echo(f"  {s.name:<12} {s.transport:<6} kind={s.source_kind:<12} {where}{flag}")


@sources_app.command("remove")
def sources_remove(name: str):
    """Disconnect a source (its OAuth tokens are deleted too)."""
    from vetromar.sources.auth import FileTokenStorage
    from vetromar.sources.registry import remove_source

    remove_source(name)
    FileTokenStorage(name).clear()
    typer.echo(f"removed {name}")


@app.command()
def sync(
    name: Optional[str] = typer.Argument(None, help="Source to sync (omit with --all)"),
    all_sources: bool = typer.Option(False, "--all", help="Sync every enabled source"),
    full: bool = typer.Option(
        False, "--full", help="Exhaustive sync: ingest the ENTIRE source, not just recent/new"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be ingested; write nothing"
    ),
    extract: bool = typer.Option(
        True, "--extract/--no-extract", help="Run generic extraction on new episodes"
    ),
):
    """Pull what's new from connected sources into the knowledge store."""
    from vetromar.sources.registry import get_source, load_sources
    from vetromar.sources.sync import sync_source

    if not name and not all_sources:
        raise ConfigError("Name a source or pass --all.", hint="`vetromar sources list` shows them.")
    targets = (
        [s for s in load_sources() if s.enabled] if all_sources else [get_source(name)]
    )
    if not targets:
        typer.echo("No enabled sources. Run `vetromar connect`.")
        return
    config = load_config()
    config.ensure_dirs()
    store = _open_store()
    for source in targets:
        report = sync_source(
            store, source, config, full=full, dry_run=dry_run, extract=extract
        )
        label = "would ingest" if dry_run else "ingested"
        typer.echo(
            f"{source.name}: {label} {len(report.created)} episode(s), "
            f"{len(report.duplicates)} duplicate(s) skipped"
            + (f", {report.units} unit(s) extracted" if not dry_run and extract else "")
        )
        for eid in report.created:
            typer.echo(f"  + {eid}")
        for eid in report.duplicates:
            typer.echo(f"  = {eid} (already in store)")
        for eid in report.extraction_failures:
            typer.secho(f"  ! extraction failed for {eid} (raw episode kept)", fg=typer.colors.YELLOW)
        if report.cursor and not dry_run:
            typer.echo(f"  cursor -> {report.cursor}")
        if report.incomplete:
            typer.secho(
                "  ! sync incomplete — fetched content is saved; run sync again to continue",
                fg=typer.colors.YELLOW,
            )


@sources_app.command("test")
def sources_test(name: str):
    """Connect to a source and list its tools."""
    from vetromar.sources import client as sources_client
    from vetromar.sources.registry import get_source

    tools = sources_client.test_source(get_source(name))
    typer.secho(f"✓ {name} reachable — {len(tools)} tool(s):", fg=typer.colors.GREEN)
    for t in tools:
        typer.echo(f"  {t}")


# -- concierge hand-entry -----------------------------------------------------


@app.command("add-episode")
def add_episode(
    title: str = typer.Argument(..., help="e.g. 'Slack #eng billing thread 2026-07-02'"),
    kind: str = typer.Option("note", "--kind", help="Source kind: email_thread / chat / document / note / ..."),
    when: Optional[str] = typer.Option(None, help="When the source event happened, ISO 8601"),
    raw_file: Optional[Path] = typer.Option(
        None, "--raw-file", exists=True, dir_okay=False,
        help="Text file with the source's raw content (evidence is validated against it)",
    ),
    raw_ref: Optional[str] = typer.Option(None, help="URL / pointer to the source material"),
):
    """Hand-enter a SOURCE episode (a Slack thread, ticket, doc you read)."""
    from vetromar.ingest.manual import add_source_episode

    store = _open_store()
    ep = add_source_episode(
        store,
        title=title,
        source_kind=kind,
        occurred_at=_parse_when(when),
        raw=raw_file.read_text() if raw_file else None,
        raw_ref=raw_ref,
    )
    typer.echo(f"created {ep.source_kind} episode {ep.id}")


@app.command("add-unit")
def add_unit(
    episode_id: str = typer.Argument(...),
    from_file: Path = typer.Option(..., "--from-file", "-f", exists=True, dir_okay=False,
                                   help="Unit JSON: a UnitDraft (any kind) or ExtractedUnit (decision) shape"),
):
    """Hand-enter one unit into an episode. Same record type as room capture.
    Every unit needs >=1 evidence item (a verbatim excerpt for text sources)."""
    from vetromar.ingest.manual import add_unit_from_file

    store = _open_store()
    unit = add_unit_from_file(store, episode_id, from_file)
    typer.echo(f"created {unit.type} unit {unit.id} in episode {episode_id}")


@app.command("new-entity")
def new_entity(
    name: str = typer.Argument(...),
    type: str = typer.Option("person", "--type", help="person / project / product / team / ..."),
):
    """Create a hand-curated entity identity."""
    from vetromar.ingest.manual import create_entity

    store = _open_store()
    e = create_entity(store, name, type=type)
    typer.echo(f"created {e.type} entity {e.id} ({e.name})")


@app.command("link-alias")
def link_alias_cmd(
    entity_id: str = typer.Argument(...),
    ref: str = typer.Argument(..., help="Speaker label or name string, e.g. SPEAKER_02 or 'Priya'"),
):
    """Hand-link a ref string to an entity — the concierge entity resolution."""
    from vetromar.ingest.manual import link_alias

    store = _open_store()
    e = link_alias(store, entity_id, ref)
    typer.echo(f"{e.name} <- {ref}  (now: {e.aliases})")


@app.command("link-units")
def link_units_cmd(
    from_unit: str = typer.Argument(...),
    to_unit: str = typer.Argument(...),
    kind: str = typer.Option("related", help="e.g. spawned / related / contradicts"),
):
    """Hand-link two units — the fusion edge (room decision -> the ticket it spawned)."""
    from vetromar.ingest.manual import link_units

    store = _open_store()
    edge = link_units(store, from_unit, to_unit, kind)
    typer.echo(f"linked {edge.from_id} -[{edge.kind}]-> {edge.to_id}")


@app.command()
def supersede(
    old_unit: str = typer.Argument(...),
    new_unit: str = typer.Argument(...),
):
    """Close validity on old_unit as of new_unit's valid_from (manual, bi-temporal)."""
    from vetromar.ingest.manual import supersede as _supersede

    store = _open_store()
    closed = _supersede(store, old_unit, new_unit)
    typer.echo(f"{closed.id} superseded (valid_to={closed.valid_to.isoformat()})")


# -- views --------------------------------------------------------------------


@app.command()
def episodes():
    """List episodes in the store."""
    store = _open_store()
    for ep in store.list_episodes():
        typer.echo(f"{ep.id}  [{ep.source_kind}]  {ep.occurred_at}  {ep.title}")


@app.command()
def units(
    type: Optional[str] = typer.Option(None, "--type", help="decision / claim / commitment / question / metric"),
    status: Optional[str] = typer.Option(None),
    episode_id: Optional[str] = typer.Option(None),
    current_only: bool = typer.Option(False, "--current-only"),
):
    """List units in the store."""
    store = _open_store()
    for u in store.list_units(type=type, status=status, episode_id=episode_id, current_only=current_only):
        payload_status = getattr(u.payload, "status", None)
        label = payload_status.value if payload_status is not None else u.type
        flag = "" if u.valid_to is None else "  (superseded)"
        typer.echo(f"{u.id}  [{label}]  {u.content}{flag}")


@app.command()
def render(episode_id: str = typer.Argument(...)):
    """Render an episode's units as markdown (deterministic view)."""
    from vetromar.render.markdown import render_units

    store = _open_store()
    episode = store.get_episode(episode_id)
    typer.echo(render_units(episode, store.list_units(episode_id=episode_id)))


@app.command()
def show(unit_id: str = typer.Argument(...)):
    """Dump one unit's full JSON with its edges."""
    store = _open_store()
    unit = store.get_unit(unit_id)
    edges = store.edges_for(unit_id)
    typer.echo(json.dumps(
        {
            "unit": json.loads(unit.model_dump_json()),
            "edges": [json.loads(e.model_dump_json()) for e in edges],
        },
        indent=2,
    ))


# -- read surface ---------------------------------------------------------------


@app.command()
def serve():
    """Run the MCP read server (stdio) — the customer's agent connects here."""
    from vetromar.mcp_server.server import main as serve_main

    serve_main()


@app.command("ui-server", hidden=True)
def ui_server(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(0, help="Bind port (0 = auto; printed as PORT=<n>)"),
):
    """Run the local HTTP API for the desktop UI (launched as the Tauri sidecar).

    Prints `PORT=<n>` on stdout once bound so the shell can find the server."""
    from vetromar.ui_server.app import run_server

    run_server(host=host, port=port)


def main() -> None:
    """Entrypoint that renders setup problems (ConfigError) and evidence-gate
    rejections without a traceback — both are the user's to fix, not bugs."""
    from vetromar.extraction.validate import ExtractionGateError

    try:
        app()
    except ConfigError as exc:
        typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.hint:
            typer.secho(f"  → {exc.hint}", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(1)
    except ExtractionGateError as exc:
        typer.secho(f"rejected by the evidence gate: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
