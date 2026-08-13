"""The local HTTP API behind the desktop UI.

Every route delegates to the existing engine — `run_pipeline`, `record_mic`,
`operations.*` — and returns JSON. It adds no capture/extraction logic of its
own; it is the desktop shell's way to reach the same code the CLI reaches.

Bound to 127.0.0.1 only. Long jobs run on background threads (see jobs.py); the
frontend polls `GET /api/jobs/{id}`. SQLite is thread-bound, so every job opens
its own `Store` inside its worker thread.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from vetromar import graphs, operations, views
from vetromar.config import VETROMAR_HOME, load_config, save_config
from vetromar.errors import ConfigError
from vetromar.graphs import GraphError
from vetromar.store import Store, StoreError
from vetromar.ui_server.jobs import Job, JobRegistry

_JOBS = JobRegistry()


class RecordStart(BaseModel):
    title: str = ""  # empty → auto-generated date/time title
    when: Optional[str] = None
    graph: Optional[str] = None  # destination graph; None → private


class RecordStop(BaseModel):
    job_id: str


class SourceConnect(BaseModel):
    name: str
    url: Optional[str] = None  # set for a custom server; omitted for catalog
    source_kind: str = "document"
    # For providers without dynamic client registration (Slack-class): the
    # customer's own OAuth app credentials, seeded before the consent flow.
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


class SourceSync(BaseModel):
    full: bool = False
    dry_run: bool = False
    extract: bool = True


class AutoSyncSettings(BaseModel):
    enabled: bool
    interval_minutes: int


class MeetingRecordStart(BaseModel):
    title: str = ""  # empty → auto-generated date/time title
    when: Optional[str] = None


class MeetingSettings(BaseModel):
    enabled: bool
    grace_seconds: int


class TranscriptionSettings(BaseModel):
    mode: str  # "auto" | "local" | "cloud"


class ProviderSetup(BaseModel):
    provider: str  # "anthropic" | "openai"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


class DeepgramSetup(BaseModel):
    api_key: str


class OnboardingUpdate(BaseModel):
    tour_done: Optional[bool] = None
    checklist_dismissed: Optional[bool] = None


class EpisodeRename(BaseModel):
    title: str


class GraphCreate(BaseModel):
    name: str


class NoteCreate(BaseModel):
    text: str
    title: Optional[str] = None


class HostConfigure(BaseModel):
    enabled: Optional[bool] = None
    port: Optional[int] = None
    advertise_url: Optional[str] = None  # "" clears the explicit choice


class HostCreateGraph(BaseModel):
    name: str
    handle: str = "host"
    display_name: str = ""


class GraphJoin(BaseModel):
    invite_url: str
    handle: str
    display_name: str = ""


class GraphInvite(BaseModel):
    role: str = "member"


class GraphRole(BaseModel):
    role: str


class ShareRequest(BaseModel):
    graph: str  # destination graph id
    from_graph: Optional[str] = None  # None → the private graph
    unit_ids: list[str] = []
    episode_ids: list[str] = []


def _config_http(exc: ConfigError, status: int = 400) -> HTTPException:
    detail = exc.message if exc.hint is None else f"{exc.message} {exc.hint}"
    return HTTPException(status_code=status, detail=detail)


def _parse_when(when: Optional[str]) -> datetime:
    return views.parse_as_of(when) or datetime.now(timezone.utc)


def _pipeline_payload(episode, units, markdown: str) -> dict:
    """A finished capture as JSON: episode + units + the markdown view.
    Episode raw (the full transcript JSON) stays server-side — the UI doesn't
    render it and it would bloat every job poll."""
    return {
        "episode": views.episode_dict(episode),
        "units": [json.loads(u.model_dump_json()) for u in units],
        "markdown": markdown,
    }


def _graph_db_path(graph: Optional[str]) -> Path:
    """graph id (query/body param) → the store's db path. None → private.
    Unknown graphs 404 — the id came from the frontend's registry list."""
    try:
        return graphs.resolve_db_path(graph)
    except GraphError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _graph_contributor(graph: Optional[str]):
    """Route-time contributor resolution for job workers (the same rule as
    db paths: resolve now, close over the value)."""
    try:
        return graphs.contributor_for(graph)
    except GraphError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _graph_meta(graph: Optional[str]) -> dict:
    """Job meta labeling: which graph a job writes into, for UI badges."""
    graph_id = graph or graphs.PRIVATE_GRAPH_ID
    try:
        name = graphs.get_graph(graph_id).name
    except GraphError:
        name = graph_id
    return {"graph": graph_id, "graph_name": name}


def _with_store(fn, graph: Optional[str] = None):
    """Run a call against a fresh Store (FastAPI sync routes run in a thread
    pool and SQLite is thread-bound — same reasoning as jobs.py). `graph`
    selects which graph's store — opened via graphs.open_store so writes
    into shared graphs carry the contributor stamp. Unknown ids (StoreError)
    become 404s."""
    try:
        store = graphs.open_store(graph)
    except GraphError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        return fn(store)
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        store.close()


def _graph_config(db_path: Optional[Path]):
    """Worker-side config: `db_path` (resolved at route time — never re-derive
    the graph inside a worker thread) overrides the private store so every
    blob dir (transcripts/, uploads/, ...) derives per-graph."""
    config = load_config()
    if db_path is not None:
        config = dataclasses.replace(config, db_path=db_path)
    config.ensure_dirs()
    return config


def _run_pipeline_on(
    job: Job,
    audio_path: Path,
    title: str,
    when: datetime,
    db_path: Optional[Path] = None,
    contributor=None,
) -> dict:
    """Shared tail of capture + record: prep backend, run the pipeline, package.

    Opens the Store inside the worker thread (SQLite is thread-bound).
    `db_path`/`contributor` are resolved at ROUTE time — a worker never
    re-derives the graph."""
    from vetromar.capture.pipeline import run_pipeline

    config = _graph_config(db_path)
    operations.ensure_backend_ready(config)
    job.set_stage("Preparing", None)
    store = Store(config.db_path)
    store.contributor = contributor
    try:
        episode, units, markdown = run_pipeline(
            audio_path,
            title=title,
            config=config,
            store=store,
            occurred_at=when,
            progress=job.set_stage,
        )
    finally:
        store.close()
    job.set_stage("Done", 100.0)
    return _pipeline_payload(episode, units, markdown)


def _run_meeting_pipeline_on(
    job: Job, audio_path: Path, title: str, when: datetime, db_path: Optional[Path] = None
) -> dict:
    """The meeting-record tail: channel-aware transcription, then the same
    stages as _run_pipeline_on. Same thread rules (fresh Store per worker)."""
    from vetromar.capture.meeting import run_meeting_pipeline

    config = _graph_config(db_path)
    operations.ensure_backend_ready(config)
    job.set_stage("Preparing", None)
    store = Store(config.db_path)
    try:
        episode, units, markdown = run_meeting_pipeline(
            audio_path,
            title=title,
            config=config,
            store=store,
            occurred_at=when,
            progress=job.set_stage,
        )
    finally:
        store.close()
    job.set_stage("Done", 100.0)
    return _pipeline_payload(episode, units, markdown)


# Meeting detection state + helper supervision. Created always (routes need
# status()), started only by run_server — the scheduler rule.
from vetromar.ui_server.meetings import MeetingMonitor  # noqa: E402

_MEETINGS = MeetingMonitor(_JOBS, pipeline_tail=_run_meeting_pipeline_on)


def create_app() -> FastAPI:
    app = FastAPI(title="Vetromar UI")
    # Local-only server; the Vite dev frontend runs on another port, so allow it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health(check_api: bool = False) -> dict:
        return operations.health_report(load_config(), check_api=check_api)

    @app.post("/api/setup/cloud")
    def setup_cloud() -> dict:
        # Cloud mode with the user's own provider (configured below/Settings).
        try:
            operations.select_api_backend(load_config())
        except ConfigError as exc:
            raise _config_http(exc)
        return {"ok": True, "backend": "api"}

    @app.get("/api/settings/provider")
    def provider_get() -> dict:
        return operations.provider_settings(load_config())

    @app.post("/api/setup/provider")
    def setup_provider(body: ProviderSetup) -> dict:
        try:
            operations.configure_provider(
                body.provider,
                api_key=body.api_key,
                base_url=body.base_url,
                model=body.model,
            )
        except operations.InvalidApiKey as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ConfigError as exc:
            raise _config_http(exc)
        return operations.provider_settings(load_config())

    @app.post("/api/setup/deepgram")
    def setup_deepgram(body: DeepgramSetup) -> dict:
        if not body.api_key.strip():
            raise HTTPException(status_code=400, detail="An API key is required.")
        try:
            operations.validate_and_save_deepgram_key(body.api_key)
        except operations.InvalidApiKey as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ConfigError as exc:
            raise _config_http(exc)
        return {"ok": True}

    @app.get("/api/settings/auto-sync")
    def auto_sync_get() -> dict:
        config = load_config()
        return {
            "enabled": config.auto_sync_enabled,
            "interval_minutes": config.auto_sync_interval_minutes,
        }

    @app.post("/api/settings/auto-sync")
    def auto_sync_set(body: AutoSyncSettings) -> dict:
        if body.interval_minutes < 5:
            raise HTTPException(
                status_code=400, detail="Sync interval must be at least 5 minutes."
            )
        save_config(
            {
                "auto_sync_enabled": body.enabled,
                "auto_sync_interval_minutes": body.interval_minutes,
            }
        )
        return {"enabled": body.enabled, "interval_minutes": body.interval_minutes}

    def _onboarding_payload() -> dict:
        # Purely local (config + registry + store) — safe to call on every
        # frontend refresh without ever blocking on the network. The admin
        # "invite a teammate" check needs a cloud call, so the checklist UI
        # stitches that client-side instead.
        from vetromar.sources.registry import load_sources

        config = load_config()
        try:
            source_names = [s.name for s in load_sources()]
        except ConfigError:
            source_names = []

        def checks(store: Store) -> dict:
            return {
                "source_synced": any(store.get_sync_state(n) for n in source_names),
                "meeting_captured": any(
                    e.source_kind == "meeting" for e in store.list_episodes()
                ),
            }

        return {
            "tour_done": config.onboarding_tour_done,
            "checklist_dismissed": config.onboarding_checklist_dismissed,
            **_with_store(checks),
        }

    @app.get("/api/onboarding")
    def onboarding_status() -> dict:
        return _onboarding_payload()

    @app.get("/api/mcp")
    def mcp_info() -> dict:
        # Pure read — the shim itself is (re)written by run_server at boot.
        return operations.mcp_access_info()

    @app.post("/api/onboarding")
    def onboarding_update(body: OnboardingUpdate) -> dict:
        updates = {}
        if body.tour_done is not None:
            updates["onboarding_tour_done"] = body.tour_done
        if body.checklist_dismissed is not None:
            updates["onboarding_checklist_dismissed"] = body.checklist_dismissed
        if updates:
            save_config(updates)
        return _onboarding_payload()

    @app.post("/api/setup/local-select")
    def setup_local_select() -> dict:
        # Persist the fully-local choice only — model downloads are a
        # separate explicit job (POST /api/models/download).
        operations.select_local_backend(load_config())
        return {"ok": True, "backend": "local"}

    @app.post("/api/models/download")
    def models_download() -> dict:
        # One global download at a time; a second click attaches to the
        # running job instead of racing the same caches.
        job, started = _JOBS.create_sync_unless_active(
            "local-models", {"source": "local-models"}, kind="download-models"
        )
        if started:

            def target(job: Job) -> dict:
                config = load_config()
                config.ensure_dirs()
                operations.download_local_models(config, progress=_job_progress(job))
                return {"downloaded": True}

            _JOBS.start(job, target)
        return {"job_id": job.id, "already_running": not started}

    @app.post("/api/capture")
    async def capture(
        file: UploadFile,
        title: str = Form(""),
        when: Optional[str] = Form(None),
        graph: Optional[str] = Form(None),
    ) -> dict:
        occurred_at = _parse_when(when)
        title = title.strip() or operations.default_meeting_title(occurred_at)
        # Destination graph resolves NOW — blob dirs derive from the store's
        # parent, so the file must land in the right graph before any work.
        db_path = _graph_db_path(graph)
        # Persist the upload with its original suffix so import_audio accepts it.
        suffix = Path(file.filename or "upload").suffix or ".wav"
        uploads = db_path.parent / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        dest = uploads / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}{suffix}"
        dest.write_bytes(await file.read())

        contributor = _graph_contributor(graph)
        job = _JOBS.create("capture", meta=_graph_meta(graph))
        _JOBS.start(
            job,
            lambda j: _run_pipeline_on(j, dest, title, occurred_at, db_path, contributor),
        )
        return {"job_id": job.id}

    @app.post("/api/documents")
    async def upload_document(
        file: UploadFile,
        title: str = Form(""),
        when: Optional[str] = Form(None),
        graph: Optional[str] = Form(None),
    ) -> dict:
        occurred_at = _parse_when(when)
        db_path = _graph_db_path(graph)
        # Persist with the original suffix — the parser dispatches on it.
        suffix = Path(file.filename or "upload").suffix.lower()
        from vetromar.ingest.documents import SUPPORTED_SUFFIXES

        if suffix not in SUPPORTED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported document type {suffix or '(none)'} — "
                f"supported: {', '.join(SUPPORTED_SUFFIXES)}",
            )
        documents_dir = db_path.parent / "documents"
        documents_dir.mkdir(parents=True, exist_ok=True)
        dest = documents_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}{suffix}"
        dest.write_bytes(await file.read())
        doc_title = title.strip() or Path(file.filename or dest.name).stem

        contributor = _graph_contributor(graph)

        def target(job: Job) -> dict:
            config = _graph_config(db_path)
            store = Store(config.db_path)  # jobs open their own Store
            store.contributor = contributor
            try:
                job.log(f"parsing {dest.name}")

                def progress(done: int, total: int) -> None:
                    job.log(f"extracting part {done}/{total}")

                episode, units = operations.ingest_document(
                    store, config, dest,
                    title=doc_title, occurred_at=occurred_at, on_progress=progress,
                )
                job.log("linking")
                return {
                    "episode_id": episode.id,
                    "title": episode.title,
                    "units": len(units),
                }
            finally:
                store.close()

        job = _JOBS.create("document", meta=_graph_meta(graph))
        _JOBS.start(job, target)
        return {"job_id": job.id}

    @app.post("/api/record/start")
    def record_start(body: RecordStart) -> dict:
        occurred_at = _parse_when(body.when)
        title = body.title.strip() or operations.default_meeting_title(occurred_at)
        db_path = _graph_db_path(body.graph)
        contributor = _graph_contributor(body.graph)
        out_path = (
            db_path.parent
            / "recordings"
            / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.wav"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        def target(job: Job) -> dict:
            from vetromar.capture.audio import record_mic

            job.status = "recording"
            job.log("recording — press Stop when done")
            audio = record_mic(out_path, stop_event=job.stop_event)
            job.status = "running"
            return _run_pipeline_on(job, audio, title, occurred_at, db_path, contributor)

        job = _JOBS.create("record", meta=_graph_meta(body.graph))
        _JOBS.start(job, target)
        return {"job_id": job.id}

    @app.post("/api/record/stop")
    def record_stop(body: RecordStop) -> dict:
        job = _JOBS.get(body.job_id)
        if job is None or job.kind not in ("record", "meeting-record"):
            raise HTTPException(status_code=404, detail="no such record job")
        job.stop_event.set()
        return job.public()

    # -- virtual-meeting capture (macOS 14.2+; detection is notify-only) ------

    @app.get("/api/meetings/status")
    def meetings_status() -> dict:
        return _MEETINGS.status()

    @app.post("/api/meetings/record")
    def meetings_record(body: MeetingRecordStart) -> dict:
        occurred_at = _parse_when(body.when)
        title = body.title.strip() or operations.default_meeting_title(occurred_at)
        try:
            job, started = _MEETINGS.start_recording(title, occurred_at)
        except ConfigError as exc:
            raise _config_http(exc)
        return {"job_id": job.id, "already_running": not started}

    @app.get("/api/settings/meetings")
    def meetings_settings_get() -> dict:
        config = load_config()
        return {
            "enabled": config.meeting_detect_enabled,
            "grace_seconds": config.meeting_grace_seconds,
            "supported": _MEETINGS.supported(),
        }

    @app.post("/api/settings/meetings")
    def meetings_settings_set(body: MeetingSettings) -> dict:
        if body.grace_seconds < 5:
            raise HTTPException(
                status_code=400, detail="Grace period must be at least 5 seconds."
            )
        save_config(
            {
                "meeting_detect_enabled": body.enabled,
                "meeting_grace_seconds": body.grace_seconds,
            }
        )
        return meetings_settings_get()

    @app.get("/api/settings/transcription")
    def transcription_settings_get() -> dict:
        from vetromar.transcription.assets import transcription_models_status
        from vetromar.transcription.base import resolve_transcription_mode

        config = load_config()
        return {
            "mode": config.transcribe,
            "effective": resolve_transcription_mode(config),
            "has_deepgram_key": bool(config.deepgram_api_key),
            "local_models_present": bool(transcription_models_status(config)["present"]),
        }

    @app.post("/api/settings/transcription")
    def transcription_settings_set(body: TranscriptionSettings) -> dict:
        mode = body.mode.strip().lower()
        if mode not in ("auto", "local", "cloud"):
            raise HTTPException(
                status_code=400,
                detail="Transcription mode must be auto, local, or cloud.",
            )
        # Cloud without a key would fail at the first capture — reject now.
        # Local without models saves fine: the response's local_models_present
        # flag drives the download nudge, and the capture-time ConfigError in
        # LocalWhisperXBackend stays the hard backstop.
        if mode == "cloud" and not load_config().deepgram_api_key:
            raise HTTPException(
                status_code=400,
                detail="Cloud transcription needs a Deepgram API key — add one "
                "in Settings first.",
            )
        save_config({"transcribe": mode})
        return transcription_settings_get()

    @app.get("/api/jobs")
    def list_jobs(kind: Optional[str] = None, active: bool = False) -> list[dict]:
        """List jobs — how the UI discovers scheduler-launched auto-syncs."""
        return [j.public() for j in _JOBS.list(kind=kind, active_only=active)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job.public()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        """Abort a pending OAuth consent wait. Connect/test jobs only — on a
        record job stop_event means "stop recording and process", which is
        POST /api/record/stop's semantic, not a cancellation."""
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        if job.kind not in ("connect", "test-source"):
            raise HTTPException(
                status_code=409, detail=f"a {job.kind} job cannot be cancelled"
            )
        job.stop_event.set()
        return job.public()

    # -- identity + per-graph sync -------------------------------------------

    @app.get("/api/identity")
    def identity_info() -> dict:
        """This machine's public key — what a host enrolls. Generated on
        first read; the private half never leaves ~/.vetromar."""
        from vetromar.identity import ensure_identity, identity_key_path

        return {
            "public_key": ensure_identity().public_key,
            "key_path": str(identity_key_path()),
        }

    @app.post("/api/graphs/{graph_id}/sync")
    def graph_sync(graph_id: str) -> dict:
        from vetromar.ui_server.workspace_jobs import start_graph_sync_job

        try:
            info = graphs.get_graph(graph_id)
        except GraphError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not info.synced:
            raise HTTPException(
                status_code=400, detail="this graph is not connected to a host"
            )
        job, started = start_graph_sync_job(_JOBS, graph_id)
        return {"job_id": job.id, "already_running": not started}

    # -- host mode (this machine serving shared graphs) ------------------------

    def _host_state():
        from vetromar.hosting.server import HOST

        return HOST

    def _advertise_url(config) -> str:
        """What invite links carry. Explicit choice first; loopback last
        resort (works for same-machine testing, nothing else)."""
        if config.host_advertise_url:
            return config.host_advertise_url.rstrip("/")
        return f"http://127.0.0.1:{config.host_port}"

    @app.get("/api/host")
    def host_status() -> dict:
        from vetromar.hosting.addresses import candidate_addresses

        config = load_config()
        return {
            "enabled": config.host_enabled,
            "running": _host_state().running,
            "port": config.host_port,
            "advertise_url": _advertise_url(config),
            "advertise_url_set": bool(config.host_advertise_url),
            "candidates": candidate_addresses(config.host_port),
        }

    @app.post("/api/host")
    def host_configure(body: HostConfigure) -> dict:
        updates: dict = {}
        if body.enabled is not None:
            updates["host_enabled"] = body.enabled
        if body.port is not None:
            if not (1024 <= body.port <= 65535):
                raise HTTPException(status_code=400, detail="Port must be 1024-65535.")
            updates["host_port"] = body.port
        if body.advertise_url is not None:
            url = body.advertise_url.strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=400,
                    detail="The address must start with http:// or https://.",
                )
            updates["host_advertise_url"] = url  # empty string clears it
        if updates:
            save_config(updates)
        config = load_config()
        host = _host_state()
        try:
            if config.host_enabled and not host.running:
                host.start(config.host_port, config.host_bind)
            elif not config.host_enabled and host.running:
                host.stop()
            elif config.host_enabled and host.running and host.port != config.host_port:
                host.stop()
                host.start(config.host_port, config.host_bind)
        except OSError as exc:  # port in use etc.
            raise HTTPException(status_code=400, detail=f"Could not start hosting: {exc}")
        return host_status()

    def _graph_client(info):
        """An authenticated client for the graph's host, as this identity."""
        from vetromar.identity import ensure_identity
        from vetromar.workspace.client import CloudClient

        client = CloudClient(info.host_url, workspace_id=info.workspace_id)
        client.login_with_key(ensure_identity())
        return client

    def _graph_call(graph_id: str, fn):
        from vetromar.workspace.client import NotSignedIn, WorkspaceError

        try:
            info = graphs.get_graph(graph_id)
        except GraphError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not info.synced:
            raise HTTPException(
                status_code=400, detail="this graph is not connected to a host"
            )
        try:
            client = _graph_client(info)
        except NotSignedIn as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            return fn(client, info)
        except NotSignedIn as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            client.close()

    @app.post("/api/host/graphs")
    def host_create_graph(body: HostCreateGraph) -> dict:
        """Create a shared graph ON THIS MACHINE: workspace on the embedded
        server + local replica, bound and registered in one motion."""
        from vetromar.identity import ensure_identity
        from vetromar.store import Store
        from vetromar.workspace.client import CloudClient, WorkspaceError
        from vetromar.workspace.engine import bind_workspace

        config = load_config()
        if not config.host_enabled or not _host_state().running:
            raise HTTPException(
                status_code=400, detail="Turn hosting on before creating a graph here."
            )
        advertise = _advertise_url(config)
        client = CloudClient(advertise, http=None)
        try:
            client.login_with_key(ensure_identity())
            ws = client.create_workspace(
                body.name.strip(),
                body.handle.strip() or "host",
                body.display_name.strip() or body.handle.strip() or "Host",
            )
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            client.close()

        try:
            info = graphs.create_graph(body.name)
        except GraphError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        graphs.update_graph(
            info.id,
            host_url=advertise,
            workspace_id=ws["workspace_id"],
            role="host",
            handle=ws["handle"],
            display_name=ws["display_name"],
        )
        # Fresh empty replica: the silent first-bind, not an upload decision.
        db_path = graphs.resolve_db_path(info.id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db_path)
        try:
            bind_workspace(store, ws["workspace_id"])
        finally:
            store.close()
        return graphs.get_graph(info.id).to_dict()

    @app.post("/api/graphs/join")
    def graphs_join(body: GraphJoin) -> dict:
        """Join a friend's graph from a pasted invite link: enroll this
        identity, create the local replica, bind, and start the first sync."""
        from urllib.parse import parse_qs, urlsplit

        from vetromar.identity import ensure_identity
        from vetromar.store import Store
        from vetromar.ui_server.workspace_jobs import start_graph_sync_job
        from vetromar.workspace.client import CloudClient, WorkspaceError
        from vetromar.workspace.engine import bind_workspace

        parts = urlsplit(body.invite_url.strip())
        token = (parse_qs(parts.query).get("token") or [None])[0]
        if not parts.scheme or not parts.netloc or not token:
            raise HTTPException(
                status_code=400,
                detail="That doesn't look like an invite link — paste the whole URL.",
            )
        host_url = f"{parts.scheme}://{parts.netloc}"
        handle = body.handle.strip()
        if not handle:
            raise HTTPException(status_code=400, detail="Pick a handle first.")

        client = CloudClient(host_url)
        try:
            joined = client.accept_invite(
                token,
                ensure_identity(),
                handle,
                body.display_name.strip() or handle.title(),
            )
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            client.close()

        info = graphs.create_graph(joined["workspace_name"])
        graphs.update_graph(
            info.id,
            host_url=host_url,
            workspace_id=joined["workspace_id"],
            role=joined["role"],
            handle=joined["handle"],
            display_name=joined["display_name"],
        )
        db_path = graphs.resolve_db_path(info.id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db_path)
        try:
            bind_workspace(store, joined["workspace_id"])
        finally:
            store.close()
        job, _ = start_graph_sync_job(_JOBS, info.id)
        return {**graphs.get_graph(info.id).to_dict(), "sync_job_id": job.id}

    # -- graph membership (works against any connected graph's host) ----------

    @app.get("/api/graphs/{graph_id}/members")
    def graph_members(graph_id: str) -> dict:
        return _graph_call(graph_id, lambda c, i: c.members())

    @app.post("/api/graphs/{graph_id}/invites")
    def graph_invite(graph_id: str, body: GraphInvite) -> dict:
        def mint(client, info):
            resp = client.create_invite(body.role)
            resp["url"] = info.host_url.rstrip("/") + resp["url_path"]
            return resp

        return _graph_call(graph_id, mint)

    @app.delete("/api/graphs/{graph_id}/members/{principal_id}")
    def graph_remove_member(graph_id: str, principal_id: str) -> dict:
        _graph_call(graph_id, lambda c, i: c.remove_member(principal_id))
        return {"ok": True}

    @app.post("/api/graphs/{graph_id}/members/{principal_id}/role")
    def graph_set_role(graph_id: str, principal_id: str, body: GraphRole) -> dict:
        return _graph_call(graph_id, lambda c, i: c.set_role(principal_id, body.role))

    # -- sources (the M10 connect/sync flow; engine calls go straight to ------
    # -- vetromar/sources/*, same seams as the CLI) ---------------------------

    @app.get("/api/sources/catalog")
    def sources_catalog() -> list[dict]:
        from vetromar.sources.auth import relay_redirect_uri
        from vetromar.sources.catalog import CATALOG
        from vetromar.sources.registry import load_sources

        try:
            connected = {s.name for s in load_sources()}
        except ConfigError as exc:
            raise _config_http(exc)
        # redirect_uri: what a customer must configure on their own OAuth app
        # for needs_client_registration entries — the UI shows it in the form.
        # It is the HTTPS relay page (Slack refuses http://localhost).
        redirect_uri = relay_redirect_uri()
        return [
            {**entry.model_dump(), "connected": entry.name in connected, "redirect_uri": redirect_uri}
            for entry in CATALOG
        ]

    @app.post("/api/sources/{name}/setup-page")
    def sources_setup_page(name: str) -> dict:
        # Opens the provider's create-an-app console in the system browser
        # (the Tauri webview has no opener plugin) — allowlisted to catalog
        # setup_urls only, never arbitrary URLs from the frontend.
        from vetromar.sources.catalog import catalog_entry

        entry = catalog_entry(name)
        if entry is None or not entry.setup_url:
            raise HTTPException(status_code=404, detail=f"No setup page for '{name}'.")
        import webbrowser

        try:
            opened = bool(webbrowser.open(entry.setup_url))
        except Exception:  # noqa: BLE001 — headless envs; the URL still renders
            opened = False
        return {"opened": opened, "url": entry.setup_url}

    @app.get("/api/sources")
    def sources_list() -> list[dict]:
        from vetromar.sources.registry import load_sources

        try:
            sources = load_sources()
        except ConfigError as exc:
            raise _config_http(exc)
        # "last synced N min ago" per source, from the store's sync_state.
        last_synced: dict[str, str] = {}
        if sources:
            config = load_config()
            config.ensure_dirs()
            store = Store(config.db_path)
            try:
                for s in sources:
                    state = store.get_sync_state(s.name)
                    if state:
                        last_synced[s.name] = state[1]
            finally:
                store.close()
        return [
            {
                "name": s.name,
                "transport": s.transport,
                "source_kind": s.source_kind,
                "enabled": s.enabled,
                "where": s.url or f"{s.command} {' '.join(s.args)}".strip(),
                "last_synced_at": last_synced.get(s.name),
            }
            for s in sources
        ]

    @app.post("/api/sources/connect")
    def sources_connect(body: SourceConnect) -> dict:
        from vetromar.sources import auth as sources_auth
        from vetromar.sources.catalog import catalog_entry
        from vetromar.sources.registry import SourceConfig

        if body.client_id:
            sources_auth.seed_client_info(body.name.strip(), body.client_id, body.client_secret)
        if body.url:
            try:
                source = SourceConfig(
                    name=body.name.strip(), url=body.url.strip(), source_kind=body.source_kind
                )
            except ValueError:
                # Pydantic's ValidationError text is developer-shaped — the
                # user just typed a bad name/url into the custom-source form.
                raise HTTPException(
                    status_code=400,
                    detail="Invalid source name or URL — names are lowercase "
                    "letters, digits, dashes or underscores.",
                )
        else:
            entry = catalog_entry(body.name)
            if entry is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{body.name}' is not in the catalog — pass a url for a custom server.",
                )
            if entry.needs_client_registration and not sources_auth.has_client_info(entry.name):
                raise HTTPException(
                    status_code=400,
                    detail=f"{entry.name} needs your own app's credentials — create one at "
                    f"{entry.setup_url} and enter its client ID and secret.",
                )
            source = SourceConfig(name=entry.name, url=entry.url, source_kind=entry.source_kind)

        job = _JOBS.create("connect")

        def target(job: Job) -> dict:
            from vetromar.sources import client as sources_client
            from vetromar.sources.registry import upsert_source

            # test_source runs the OAuth dance on first contact: the sidecar
            # opens the system browser and waits on the loopback callback.
            # job.stop_event (set by POST /api/jobs/{id}/cancel) aborts the wait.
            job.set_stage("Complete the consent in your browser…", None)
            tools = sources_client.test_source(source, cancel_event=job.stop_event)
            upsert_source(source)
            job.set_stage("Connected", None)
            return {"name": source.name, "tools": tools}

        _JOBS.start(job, target)
        return {"job_id": job.id}

    @app.post("/api/sources/{name}/test")
    def sources_test(name: str) -> dict:
        from vetromar.sources.registry import get_source

        try:
            source = get_source(name)
        except ConfigError as exc:
            raise _config_http(exc, status=404)

        job = _JOBS.create("test-source")

        def target(job: Job) -> dict:
            from vetromar.sources import client as sources_client

            # Expired tokens can re-trigger the browser consent, hence a job.
            job.set_stage("Testing connection…", None)
            return {
                "name": name,
                "tools": sources_client.test_source(source, cancel_event=job.stop_event),
            }

        _JOBS.start(job, target)
        return {"job_id": job.id}

    @app.delete("/api/sources/{name}")
    def sources_remove(name: str) -> dict:
        from vetromar.sources.auth import FileTokenStorage
        from vetromar.sources.registry import remove_source

        try:
            remove_source(name)
        except ConfigError as exc:
            raise _config_http(exc, status=404)
        FileTokenStorage(name).clear()
        return {"ok": True}

    @app.post("/api/sources/{name}/sync")
    def sources_sync(name: str, body: SourceSync) -> dict:
        from vetromar.sources.registry import get_source
        from vetromar.ui_server.sync_jobs import start_sync_job

        try:
            source = get_source(name)
        except ConfigError as exc:
            raise _config_http(exc, status=404)

        job, started = start_sync_job(
            _JOBS,
            source,
            full=body.full,
            dry_run=body.dry_run,
            extract=body.extract,
        )
        # already_running: a sync (manual or auto) is in flight for this source —
        # the UI attaches to it instead of double-syncing.
        return {"job_id": job.id, "already_running": not started}

    # -- graphs (the multi-graph surface: private + shared; registry in ------
    # -- vetromar/graphs.py) ---------------------------------------------------

    @app.get("/api/graphs")
    def graphs_list() -> list[dict]:
        return [g.to_dict() for g in graphs.list_graphs()]

    @app.post("/api/graphs")
    def graphs_create(body: GraphCreate) -> dict:
        try:
            info = graphs.create_graph(body.name)
        except GraphError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Touch the store so the graph is queryable immediately (migrations run).
        _with_store(lambda s: None, graph=info.id)
        return info.to_dict()

    @app.delete("/api/graphs/{graph_id}")
    def graphs_remove(graph_id: str, delete_files: bool = False) -> dict:
        try:
            graphs.remove_graph(graph_id, delete_files=delete_files)
        except GraphError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True}

    @app.post("/api/graphs/{graph_id}/note")
    def graphs_note(graph_id: str, body: NoteCreate) -> dict:
        from vetromar.ingest.notes import add_quick_note

        if not body.text.strip():
            raise HTTPException(status_code=400, detail="Note text cannot be empty.")

        def write(store: Store) -> dict:
            episode, unit = add_quick_note(store, body.text, title=body.title)
            return {
                "episode": views.episode_dict(episode),
                "unit": json.loads(unit.model_dump_json()),
            }

        return _with_store(write, graph=graph_id)

    @app.post("/api/store/share")
    def store_share(body: ShareRequest) -> dict:
        """The membrane: push selected episodes/units from one graph into
        another (default source: the private graph). Synchronous — a local
        copy is fast; the UI kicks a sync afterwards."""
        if not body.unit_ids and not body.episode_ids:
            raise HTTPException(status_code=400, detail="Select something to share.")
        if (body.from_graph or graphs.PRIVATE_GRAPH_ID) == body.graph:
            raise HTTPException(status_code=400, detail="Source and destination match.")
        try:
            src = graphs.open_store(body.from_graph)
        except GraphError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        try:
            dst = graphs.open_store(body.graph)
        except GraphError as exc:
            src.close()
            raise HTTPException(status_code=404, detail=str(exc))
        try:
            report = operations.share_to_graph(
                src, dst, unit_ids=body.unit_ids, episode_ids=body.episode_ids
            )
        except StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        finally:
            src.close()
            dst.close()
        return report

    # -- store browsing (same payloads as the MCP read tools; the lone
    # mutation is the episode-title rename — units stay read-only) ------------

    @app.get("/api/store/search")
    def store_search(
        text: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        episode_id: Optional[str] = None,
        method: Optional[str] = None,
        current_only: bool = False,
        as_of: Optional[str] = None,
        k: int = 25,
        graph: Optional[str] = None,
    ) -> list[dict]:
        return _with_store(
            lambda s: views.search_units(
                s,
                text,
                type=type,
                status=status,
                episode_id=episode_id,
                method=method,
                current_only=current_only,
                as_of=as_of,
                k=k,
                with_labels=True,
            ),
            graph=graph,
        )

    @app.get("/api/store/units/{unit_id}")
    def store_unit(unit_id: str, graph: Optional[str] = None) -> dict:
        return _with_store(
            lambda s: views.unit_payload(s, s.get_unit(unit_id), with_labels=True),
            graph=graph,
        )

    @app.get("/api/store/episodes")
    def store_episodes(
        limit: Optional[int] = None, offset: int = 0, graph: Optional[str] = None
    ) -> list[dict]:
        return _with_store(
            lambda s: [
                views.episode_dict(e)
                for e in s.list_episodes(limit=limit, offset=offset)
            ],
            graph=graph,
        )

    @app.get("/api/store/episodes/{episode_id}")
    def store_episode(
        episode_id: str, include_raw: bool = False, graph: Optional[str] = None
    ) -> dict:
        return _with_store(
            lambda s: views.episode_detail(s, episode_id, include_raw=include_raw),
            graph=graph,
        )

    @app.post("/api/store/episodes/{episode_id}/rename")
    def store_episode_rename(
        episode_id: str, body: EpisodeRename, graph: Optional[str] = None
    ) -> dict:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty.")
        return _with_store(
            lambda s: views.episode_dict(s.rename_episode(episode_id, title)),
            graph=graph,
        )

    @app.get("/api/store/entities")
    def store_entities(
        type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        graph: Optional[str] = None,
    ) -> list[dict]:
        return _with_store(
            lambda s: [
                json.loads(e.model_dump_json())
                for e in s.list_entities(type=type, limit=limit, offset=offset)
            ],
            graph=graph,
        )

    @app.get("/api/store/entities/{entity_id}/units")
    def store_entity_units(entity_id: str, graph: Optional[str] = None) -> list[dict]:
        return _with_store(
            lambda s: [
                views.unit_payload(s, u, with_labels=True)
                for u in s.units_by_entity(entity_id)
            ],
            graph=graph,
        )

    @app.get("/api/store/context")
    def store_context(
        query: str,
        token_budget: int = 2000,
        as_of: Optional[str] = None,
        graph: Optional[str] = None,
    ) -> dict:
        from vetromar.context import build_context

        return _with_store(
            lambda s: build_context(
                s, query, token_budget=token_budget, as_of=views.parse_as_of(as_of)
            ),
            graph=graph,
        )

    @app.post("/api/store/dedupe")
    def store_dedupe(graph: Optional[str] = None) -> dict:
        """Merge duplicate entities (redirect-based, history preserved). One
        run at a time per graph — a second click attaches to the running job."""
        graph_id = graph or graphs.PRIVATE_GRAPH_ID
        db_path = _graph_db_path(graph)
        job, started = _JOBS.create_sync_unless_active(
            f"entity-dedupe:{graph_id}",
            {"source": f"entity-dedupe:{graph_id}", **_graph_meta(graph)},
            kind="dedupe",
        )
        if started:

            def target(job: Job) -> dict:
                from vetromar.linking.dedupe import dedupe_entities

                config = _graph_config(db_path)
                store = Store(config.db_path)  # jobs open their own Store
                try:
                    report = dedupe_entities(store, config)
                finally:
                    store.close()
                return {
                    "merged": report.merged,
                    "pairs_judged": report.llm_pairs_judged,
                    "errors": report.errors,
                }

            _JOBS.start(job, target)
        return {"job_id": job.id, "already_running": not started}

    @app.get("/api/store/current")
    def store_current(entity_id: Optional[str] = None, graph: Optional[str] = None) -> dict:
        return _with_store(lambda s: views.current_state(s, entity_id), graph=graph)

    @app.get("/api/store/graph")
    def store_graph(
        seed: Optional[str] = None,
        hops: int = 2,
        limit: int = 500,
        graph: Optional[str] = None,
    ) -> dict:
        return _with_store(
            lambda s: views.graph(s, seed=seed, hops=hops, limit=limit),
            graph=graph,
        )

    _mount_frontend(app)
    return app


def _job_progress(job: Job):
    """Adapt runtime's (label, completed, total) progress to job.progress,
    throttled so a streaming model pull doesn't append thousands of lines."""
    state = {"label": None, "bucket": -1}

    def report(label: str, completed: "int | None", total: "int | None") -> None:
        if total:
            bucket = int(completed / total * 100) // 5  # 5% steps
            if label == state["label"] and bucket == state["bucket"]:
                return
            state["label"], state["bucket"] = label, bucket
            job.progress.append(f"{label}: {int(completed / total * 100)}%")
        elif label != state["label"]:
            state["label"], state["bucket"] = label, -1
            job.progress.append(f"{label}…")

    return report


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA if present (VETROMAR_UI_DIST, else desktop/frontend/dist).

    In `tauri dev` the frontend is served by Vite, so a missing dist is fine."""
    import os

    from fastapi.staticfiles import StaticFiles

    dist = os.environ.get("VETROMAR_UI_DIST")
    dist_dir = Path(dist) if dist else Path(__file__).resolve().parents[2] / "desktop" / "frontend" / "dist"
    if dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="frontend")


def run_server(host: str = "127.0.0.1", port: int = 0) -> None:
    """Start uvicorn. `port=0` binds a free port; the chosen port is printed as
    `PORT=<n>` on stdout so the Tauri shell can read it and point the webview."""
    import uvicorn

    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
    # Contract with the desktop shell: this exact line, flushed, before serving.
    print(f"PORT={port}", flush=True)
    sys.stdout.flush()
    # Persist logs: the bundled app's stderr goes nowhere (LaunchServices), so
    # a job traceback (the detail behind every VM-100) would be lost without
    # this. Best-effort — a read-only home must not block the app.
    try:
        import logging
        from logging.handlers import RotatingFileHandler

        log_path = VETROMAR_HOME / "logs" / "sidecar.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        logging.getLogger(__name__).info("sidecar starting on port %s", port)
    except OSError:
        pass
    # Keep the agent-facing MCP shim pointing at THIS engine (real server only —
    # create_app()/tests never touch the user's ~/.vetromar). Best-effort: a
    # read-only home must not block the app.
    try:
        operations.install_mcp_shim()
    except OSError:
        pass
    # Background schedulers live with the real server only — create_app() (and
    # thus tests/TestClient) never starts them.
    from vetromar.ui_server.scheduler import SyncScheduler
    from vetromar.ui_server.workspace_scheduler import WorkspaceSyncScheduler

    scheduler = SyncScheduler(_JOBS)
    scheduler.start()
    ws_scheduler = WorkspaceSyncScheduler(_JOBS)
    ws_scheduler.start()
    _MEETINGS.start()
    # Host mode: serve shared graphs from this machine (real server only —
    # same rule as the schedulers). Failure must not block the app.
    host_server = None
    boot_config = load_config()
    if boot_config.host_enabled:
        try:
            from vetromar.hosting.server import HOST

            HOST.start(boot_config.host_port, boot_config.host_bind)
            host_server = HOST
        except Exception:  # noqa: BLE001 — port taken, etc.; the UI shows state
            import logging as _logging

            _logging.getLogger(__name__).exception("embedded graph host failed to start")
    try:
        uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    finally:
        scheduler.stop()
        ws_scheduler.stop()
        _MEETINGS.stop()
        if host_server is not None:
            host_server.stop()


app = create_app()
