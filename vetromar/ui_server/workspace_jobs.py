"""Shared launcher for workspace-sync jobs — one seam for the manual route
AND the background scheduler (the sync_jobs.py pattern). One global guard key:
two workspace syncs must never overlap (the pull cursor is read-modify-write).
The worker opens its own Store (SQLite is thread-bound)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from vetromar.config import load_config
from vetromar.store import Store
from vetromar.ui_server.jobs import Job, JobRegistry
from vetromar.ui_server.sync_jobs import _JobLogHandler

logger = logging.getLogger(__name__)

_GUARD_KEY = "workspace"


def start_workspace_sync_job(
    registry: JobRegistry, *, initiator: str = "manual"
) -> tuple[Job, bool]:
    job, started = registry.create_sync_unless_active(
        _GUARD_KEY,
        {"source": _GUARD_KEY, "initiator": initiator},
        kind="workspace-sync",
    )
    if not started:
        return job, False

    def target(job: Job) -> dict:
        from vetromar.workspace import auth as ws_auth
        from vetromar.workspace.client import CloudClient, NotSignedIn
        from vetromar.workspace.engine import sync_workspace

        config = load_config()
        config.ensure_dirs()
        if not config.cloud_token:
            raise NotSignedIn("not signed in to a workspace")
        job.set_stage("Syncing workspace…", None)
        ws_logger = logging.getLogger("vetromar.workspace.sync")
        handler = _JobLogHandler(job)
        old_level = ws_logger.level
        ws_logger.addHandler(handler)
        ws_logger.setLevel(logging.INFO)
        client = CloudClient(config.cloud_api_url, token=config.cloud_token)
        store = Store(config.db_path)
        try:
            report = sync_workspace(
                store,
                client,
                ws_auth.device_id(),
                workspace_id=ws_auth.status().get("workspace_id"),
            )
        finally:
            ws_logger.removeHandler(handler)
            ws_logger.setLevel(old_level)
            store.close()
            client.close()
        ws_auth.note_synced(datetime.now(timezone.utc).isoformat())
        job.set_stage("Done", None)
        return report.as_dict()

    registry.start(job, target)
    return job, True
