"""The local HTTP API behind the desktop UI.

Every route delegates to the existing engine — `run_pipeline`, `record_mic`,
`operations.*` — and returns JSON. It adds no capture/extraction logic of its
own; it is the desktop shell's way to reach the same code the CLI reaches.

Bound to 127.0.0.1 only. Long jobs run on background threads (see jobs.py); the
frontend polls `GET /api/jobs/{id}`. SQLite is thread-bound, so every job opens
its own `Store` inside its worker thread.
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from vetromar import operations, views
from vetromar.config import load_config, save_config
from vetromar.errors import ConfigError
from vetromar.store import Store, StoreError
from vetromar.ui_server.jobs import Job, JobRegistry

_JOBS = JobRegistry()


class RecordStart(BaseModel):
    title: str = ""  # empty → auto-generated date/time title
    when: Optional[str] = None


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


class WorkspaceSignIn(BaseModel):
    email: str
    password: str


class WorkspaceInvite(BaseModel):
    role: str = "member"
    email: Optional[str] = None


class WorkspaceResetRequest(BaseModel):
    email: str


class WebsiteOpen(BaseModel):
    path: str = "/"


class EpisodeRename(BaseModel):
    title: str


class ConfirmPassword(BaseModel):
    password: str


class BindingAction(BaseModel):
    action: str  # 'upload' is the only action today


class ServerUrl(BaseModel):
    url: str


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


def _with_store(fn):
    """Run a read against a fresh Store (FastAPI sync routes run in a thread
    pool and SQLite is thread-bound — same reasoning as jobs.py). Unknown ids
    (StoreError) become 404s."""
    config = load_config()
    config.ensure_dirs()
    store = Store(config.db_path)
    try:
        return fn(store)
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        store.close()


def _run_pipeline_on(job: Job, audio_path: Path, title: str, when: datetime) -> dict:
    """Shared tail of capture + record: prep backend, run the pipeline, package.

    Opens the Store inside the worker thread (SQLite is thread-bound)."""
    from vetromar.capture.pipeline import run_pipeline

    config = load_config()
    config.ensure_dirs()
    operations.ensure_backend_ready(config)
    job.set_stage("Preparing", None)
    store = Store(config.db_path)
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
    ) -> dict:
        occurred_at = _parse_when(when)
        title = title.strip() or operations.default_meeting_title(occurred_at)
        config = load_config()
        config.ensure_dirs()
        # Persist the upload with its original suffix so import_audio accepts it.
        suffix = Path(file.filename or "upload").suffix or ".wav"
        uploads = config.db_path.parent / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        dest = uploads / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}{suffix}"
        dest.write_bytes(await file.read())

        job = _JOBS.create("capture")
        _JOBS.start(job, lambda j: _run_pipeline_on(j, dest, title, occurred_at))
        return {"job_id": job.id}

    @app.post("/api/documents")
    async def upload_document(
        file: UploadFile,
        title: str = Form(""),
        when: Optional[str] = Form(None),
    ) -> dict:
        occurred_at = _parse_when(when)
        config = load_config()
        config.ensure_dirs()
        # Persist with the original suffix — the parser dispatches on it.
        suffix = Path(file.filename or "upload").suffix.lower()
        from vetromar.ingest.documents import SUPPORTED_SUFFIXES

        if suffix not in SUPPORTED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported document type {suffix or '(none)'} — "
                f"supported: {', '.join(SUPPORTED_SUFFIXES)}",
            )
        documents_dir = config.db_path.parent / "documents"
        documents_dir.mkdir(parents=True, exist_ok=True)
        dest = documents_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}{suffix}"
        dest.write_bytes(await file.read())
        doc_title = title.strip() or Path(file.filename or dest.name).stem

        def target(job: Job) -> dict:
            store = Store(config.db_path)  # jobs open their own Store
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

        job = _JOBS.create("document")
        _JOBS.start(job, target)
        return {"job_id": job.id}

    @app.post("/api/record/start")
    def record_start(body: RecordStart) -> dict:
        occurred_at = _parse_when(body.when)
        title = body.title.strip() or operations.default_meeting_title(occurred_at)
        config = load_config()
        config.ensure_dirs()
        out_path = (
            config.db_path.parent
            / "recordings"
            / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.wav"
        )

        def target(job: Job) -> dict:
            from vetromar.capture.audio import record_mic

            job.status = "recording"
            job.log("recording — press Stop when done")
            audio = record_mic(out_path, stop_event=job.stop_event)
            job.status = "running"
            return _run_pipeline_on(job, audio, title, occurred_at)

        job = _JOBS.create("record")
        _JOBS.start(job, target)
        return {"job_id": job.id}

    @app.post("/api/record/stop")
    def record_stop(body: RecordStop) -> dict:
        job = _JOBS.get(body.job_id)
        if job is None or job.kind != "record":
            raise HTTPException(status_code=404, detail="no such record job")
        job.stop_event.set()
        return job.public()

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

    # -- workspace (M14: required sign-in, team management, multi-device -----
    # -- sync; engine calls go to vetromar/workspace/*) -----------------------

    def _cloud_client():
        from vetromar.workspace.client import CloudClient

        config = load_config()
        if not config.cloud_token:
            raise HTTPException(status_code=401, detail="not signed in")
        return CloudClient(config.cloud_api_url, token=config.cloud_token)

    def _workspace_call(fn):
        """Run a cloud call, mapping workspace errors to HTTP statuses the
        frontend understands (401 → back to sign-in)."""
        from vetromar.workspace.client import NotSignedIn, WorkspaceError

        client = _cloud_client()
        try:
            return fn(client)
        except NotSignedIn as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            client.close()

    @app.get("/api/workspace")
    def workspace_status(refresh: bool = False) -> dict:
        from vetromar.workspace import auth as ws_auth

        status = ws_auth.refresh_status() if refresh else ws_auth.status()
        if status["signed_in"]:
            status["quarantine_count"] = _with_store(lambda s: s.quarantine_count())
        # The sign-in screen shows/edits the workspace server URL and links
        # its /signup page.
        status["server_url"] = load_config().cloud_api_url
        return status

    @app.post("/api/workspace/server-url")
    def workspace_server_url(body: ServerUrl) -> dict:
        url = body.url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=400,
                detail="The server URL must start with http:// or https://.",
            )
        save_config({"cloud_api_url": url})
        return {"url": url}

    @app.post("/api/workspace/open-signup")
    def workspace_open_signup() -> dict:
        # Open the workspace server's own signup page in the system browser
        # (the Tauri webview has no opener plugin).
        import webbrowser

        url = load_config().cloud_api_url.rstrip("/") + "/signup"
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 — headless envs; the URL still renders
            opened = False
        return {"url": url, "opened": opened}

    @app.post("/api/workspace/signin")
    def workspace_signin(body: WorkspaceSignIn) -> dict:
        from vetromar.workspace import auth as ws_auth
        from vetromar.workspace.client import WorkspaceError

        try:
            return ws_auth.sign_in(body.email, body.password)
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/workspace/signout")
    def workspace_signout() -> dict:
        from vetromar.workspace import auth as ws_auth

        ws_auth.sign_out()
        return {"ok": True}

    @app.get("/api/workspace/members")
    def workspace_members() -> dict:
        return _workspace_call(lambda c: c.members())

    @app.delete("/api/workspace/members/{user_id}")
    def workspace_remove_member(user_id: str) -> dict:
        _workspace_call(lambda c: c.remove_member(user_id))
        return {"ok": True}

    @app.post("/api/workspace/invites")
    def workspace_invite(body: WorkspaceInvite) -> dict:
        config = load_config()
        resp = _workspace_call(lambda c: c.create_invite(body.role, email=body.email))
        # The copyable link: the server's own invite-accept page + the
        # one-time token.
        resp["url"] = config.cloud_api_url.rstrip("/") + resp["url_path"]
        return resp

    @app.post("/api/workspace/reset-request")
    def workspace_reset_request(body: WorkspaceResetRequest) -> dict:
        # Unauthenticated by design — it's the signed-out "forgot password" path.
        from vetromar.workspace.client import CloudClient, WorkspaceError

        config = load_config()
        client = CloudClient(config.cloud_api_url)
        try:
            return client.reset_request(body.email)
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            client.close()

    @app.post("/api/website/open")
    def website_open(body: WebsiteOpen) -> dict:
        # The Tauri webview has no opener plugin, so target="_blank" anchors go
        # nowhere — the sidecar opens the system browser instead. Only
        # website pages, never arbitrary
        # URLs: the path is joined onto the configured website base.
        import webbrowser

        config = load_config()
        path = body.path if body.path.startswith("/") else "/" + body.path
        url = config.website_base_url.rstrip("/") + path
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 — headless envs; the URL still renders
            opened = False
        return {"url": url, "opened": opened}

    @app.get("/api/workspace/binding")
    def workspace_binding() -> dict:
        """Does this machine's store belong to the signed-in workspace?
        'needs_decision' drives the upload-vs-hold-off banner."""
        from vetromar.workspace import auth as ws_auth
        from vetromar.workspace.engine import BOUND_KEY, binding_status

        status = ws_auth.status()
        if not status["signed_in"]:
            raise HTTPException(status_code=401, detail="not signed in")
        ws_id = status["workspace_id"]
        return _with_store(
            lambda s: {
                "status": binding_status(s, ws_id),
                "bound_workspace": s.get_replication_state(BOUND_KEY),
                "workspace_id": ws_id,
            }
        )

    @app.post("/api/workspace/binding")
    def workspace_binding_decide(body: BindingAction) -> dict:
        from vetromar.workspace import auth as ws_auth
        from vetromar.workspace.engine import rebind_and_upload

        if body.action != "upload":
            raise HTTPException(status_code=400, detail="unknown binding action")
        status = ws_auth.status()
        if not status["signed_in"]:
            raise HTTPException(status_code=401, detail="not signed in")
        ws_id = status["workspace_id"]
        requeued = _with_store(lambda s: rebind_and_upload(s, ws_id))
        return {"ok": True, "requeued": requeued}

    @app.post("/api/workspace/delete")
    def workspace_delete(body: ConfirmPassword) -> dict:
        """Admin-only on the cloud side. On success the local session is
        cleared; the LOCAL knowledge store is deliberately untouched."""
        from vetromar.workspace import auth as ws_auth

        _workspace_call(lambda c: c.delete_workspace(body.password))
        ws_auth.sign_out()
        return {"deleted": True}

    @app.post("/api/account/delete")
    def account_delete(body: ConfirmPassword) -> dict:
        from vetromar.workspace import auth as ws_auth

        _workspace_call(lambda c: c.delete_account(body.password))
        ws_auth.sign_out()
        return {"deleted": True}

    @app.post("/api/workspace/sync")
    def workspace_sync() -> dict:
        from vetromar.ui_server.workspace_jobs import start_workspace_sync_job

        config = load_config()
        if not config.cloud_token:
            raise HTTPException(status_code=401, detail="not signed in")
        job, started = start_workspace_sync_job(_JOBS)
        return {"job_id": job.id, "already_running": not started}

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
        # (the website/open pattern) — allowlisted to catalog setup_urls only,
        # never arbitrary URLs from the frontend.
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
            )
        )

    @app.get("/api/store/units/{unit_id}")
    def store_unit(unit_id: str) -> dict:
        return _with_store(
            lambda s: views.unit_payload(s, s.get_unit(unit_id), with_labels=True)
        )

    @app.get("/api/store/episodes")
    def store_episodes(limit: Optional[int] = None, offset: int = 0) -> list[dict]:
        return _with_store(
            lambda s: [
                views.episode_dict(e)
                for e in s.list_episodes(limit=limit, offset=offset)
            ]
        )

    @app.get("/api/store/episodes/{episode_id}")
    def store_episode(episode_id: str, include_raw: bool = False) -> dict:
        return _with_store(
            lambda s: views.episode_detail(s, episode_id, include_raw=include_raw)
        )

    @app.post("/api/store/episodes/{episode_id}/rename")
    def store_episode_rename(episode_id: str, body: EpisodeRename) -> dict:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty.")
        return _with_store(
            lambda s: views.episode_dict(s.rename_episode(episode_id, title))
        )

    @app.get("/api/store/entities")
    def store_entities(
        type: Optional[str] = None, limit: Optional[int] = None, offset: int = 0
    ) -> list[dict]:
        return _with_store(
            lambda s: [
                json.loads(e.model_dump_json())
                for e in s.list_entities(type=type, limit=limit, offset=offset)
            ]
        )

    @app.get("/api/store/entities/{entity_id}/units")
    def store_entity_units(entity_id: str) -> list[dict]:
        return _with_store(
            lambda s: [
                views.unit_payload(s, u, with_labels=True)
                for u in s.units_by_entity(entity_id)
            ]
        )

    @app.get("/api/store/context")
    def store_context(
        query: str, token_budget: int = 2000, as_of: Optional[str] = None
    ) -> dict:
        from vetromar.context import build_context

        return _with_store(
            lambda s: build_context(
                s, query, token_budget=token_budget, as_of=views.parse_as_of(as_of)
            )
        )

    @app.post("/api/store/dedupe")
    def store_dedupe() -> dict:
        """Merge duplicate entities (redirect-based, history preserved). One
        global run at a time — a second click attaches to the running job."""
        job, started = _JOBS.create_sync_unless_active(
            "entity-dedupe", {"source": "entity-dedupe"}, kind="dedupe"
        )
        if started:

            def target(job: Job) -> dict:
                from vetromar.linking.dedupe import dedupe_entities

                config = load_config()
                config.ensure_dirs()
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
    def store_current(entity_id: Optional[str] = None) -> dict:
        return _with_store(lambda s: views.current_state(s, entity_id))

    @app.get("/api/store/graph")
    def store_graph(
        seed: Optional[str] = None, hops: int = 2, limit: int = 500
    ) -> dict:
        return _with_store(
            lambda s: views.graph(s, seed=seed, hops=hops, limit=limit)
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
    try:
        uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    finally:
        scheduler.stop()
        ws_scheduler.stop()


app = create_app()
