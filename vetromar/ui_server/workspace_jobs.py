"""Shared launcher for per-graph sync jobs — one seam for the manual route
AND the background scheduler (the sync_jobs.py pattern). One guard key PER
GRAPH: two syncs of the same graph must never overlap (its pull cursor is
read-modify-write), while different graphs sync freely in parallel.
The worker opens its own Store (SQLite is thread-bound)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from vetromar import graphs
from vetromar.ui_server.jobs import Job, JobRegistry
from vetromar.ui_server.sync_jobs import _JobLogHandler

logger = logging.getLogger(__name__)


def _guard_key(graph_id: str) -> str:
    return f"graph:{graph_id}"


def start_graph_sync_job(
    registry: JobRegistry, graph_id: str, *, initiator: str = "manual"
) -> tuple[Job, bool]:
    info = graphs.get_graph(graph_id)  # GraphError propagates to the route
    job, started = registry.create_sync_unless_active(
        _guard_key(graph_id),
        {
            "source": _guard_key(graph_id),
            "initiator": initiator,
            "graph": info.id,
            "graph_name": info.name,
        },
        kind="workspace-sync",
    )
    if not started:
        return job, False

    def target(job: Job) -> dict:
        from vetromar.identity import ensure_identity
        from vetromar.store import Store
        from vetromar.workspace import auth as ws_auth
        from vetromar.workspace.client import CloudClient, WorkspaceError
        from vetromar.workspace.engine import sync_workspace

        # Re-resolve inside the worker: the entry may have gained/lost its
        # host between scheduling and running. The db PATH still comes from
        # the registry snapshot — graphs never move on disk.
        current = graphs.get_graph(graph_id)
        if not current.synced:
            raise WorkspaceError("this graph is not connected to a host")
        job.set_stage("Syncing graph…", None)
        ws_logger = logging.getLogger("vetromar.workspace.sync")
        handler = _JobLogHandler(job)
        old_level = ws_logger.level
        ws_logger.addHandler(handler)
        ws_logger.setLevel(logging.INFO)
        client = CloudClient(current.host_url, workspace_id=current.workspace_id)
        db_path = graphs.resolve_db_path(graph_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db_path)
        try:
            client.login_with_key(ensure_identity())
            report = sync_workspace(
                store,
                client,
                ws_auth.device_id(),
                workspace_id=current.workspace_id,
            )
        finally:
            ws_logger.removeHandler(handler)
            ws_logger.setLevel(old_level)
            store.close()
            client.close()
        graphs.update_graph(
            graph_id, last_synced_at=datetime.now(timezone.utc).isoformat()
        )
        job.set_stage("Done", None)
        return report.as_dict()

    registry.start(job, target)
    return job, True
