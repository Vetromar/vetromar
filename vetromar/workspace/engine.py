"""The sync engine: push pending outbox changes, pull the workspace log,

apply in seq order. The pull cursor advances only after a page has been
applied, and push acks are recorded before the next batch — so a crash at
any point re-runs safely (server dedupes change_ids; apply is idempotent).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from vetromar.store import Store

from .bootstrap import ensure_seeded
from .client import CloudClient, WorkspaceBindingError

logger = logging.getLogger("vetromar.workspace.sync")

PUSH_BATCH = 200
CURSOR_KEY = "pull_cursor"
BOUND_KEY = "bound_workspace"


def binding_status(store: Store, workspace_id: str) -> str:
    """'ok' when this store may sync with `workspace_id`; 'needs_decision'
    when local knowledge would land in a workspace a human never approved
    (solo-era capture, or a store that last synced elsewhere) — they must
    choose upload-vs-hold-off first."""
    bound = store.get_replication_state(BOUND_KEY)
    if bound == workspace_id:
        return "ok"
    if bound is None:
        # Unbound store: pushed rows prove it synced *somewhere* once, and
        # local rows mean a solo graph would bulk-upload on first sync —
        # either way, ask. Only a fresh empty store binds silently.
        if store.has_pushed_changes() or store.has_local_rows():
            return "needs_decision"
        return "ok"
    return "needs_decision"


def bind_workspace(store: Store, workspace_id: str) -> None:
    store.set_replication_state(BOUND_KEY, workspace_id)


def rebind_and_upload(store: Store, workspace_id: str) -> int:
    """The human chose 'upload': requeue the whole outbox for the new
    workspace, rewind the pull cursor, and bind. Returns requeued count."""
    requeued = store.reset_replication_for_rebind()
    bind_workspace(store, workspace_id)
    logger.info(
        "rebound store to workspace %s: %d change(s) requeued for upload",
        workspace_id,
        requeued,
    )
    return requeued


@dataclass
class WorkspaceSyncReport:
    pushed: int = 0
    pulled: int = 0
    applied: int = 0
    skipped: int = 0
    quarantined: int = 0
    quarantine_cleared: int = 0
    seeded: int = 0
    cursor: int = 0
    finished_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def sync_workspace(
    store: Store,
    client: CloudClient,
    device_id: str,
    workspace_id: str | None = None,
) -> WorkspaceSyncReport:
    report = WorkspaceSyncReport()

    if workspace_id is not None:
        if binding_status(store, workspace_id) != "ok":
            raise WorkspaceBindingError(
                "this machine's knowledge store isn't part of this workspace "
                "yet (it was used on its own, or synced with a different "
                "workspace) — choose what to do in the Workspace tab before "
                "syncing"
            )
        bind_workspace(store, workspace_id)

    report.seeded = ensure_seeded(store)

    # Quarantined changes first — their dependencies may have arrived since.
    report.quarantine_cleared = store.retry_quarantine()
    if report.quarantine_cleared:
        logger.info("quarantine: %d change(s) cleared", report.quarantine_cleared)

    # Push everything pending, oldest first.
    while True:
        batch = store.pending_changes(PUSH_BATCH)
        if not batch:
            break
        resp = client.push(device_id, batch)
        store.mark_pushed([c["change_id"] for c in batch])
        report.pushed += len(batch)
        logger.info(
            "push: %d change(s) sent (%d new, %d already known)",
            len(batch),
            resp.get("accepted", 0),
            resp.get("duplicates", 0),
        )

    # Pull since our cursor; advance it per applied page.
    since = int(store.get_replication_state(CURSOR_KEY) or 0)
    while True:
        resp = client.pull(since)
        changes = resp["changes"]
        for env in changes:
            report.pulled += 1
            if env.get("origin_device_id") == device_id:
                report.skipped += 1
                continue
            outcome = store.apply_change(env)
            if outcome == "applied":
                report.applied += 1
            elif outcome.startswith("quarantined"):
                report.quarantined += 1
            else:
                report.skipped += 1
        since = int(resp["next_since"])
        store.set_replication_state(CURSOR_KEY, str(since))
        if changes:
            logger.info("pull: %d change(s) at seq %d", len(changes), since)
        if not resp["has_more"]:
            break

    report.cursor = since
    logger.info(
        "workspace sync done: pushed %d, applied %d, skipped %d, quarantined %d",
        report.pushed,
        report.applied,
        report.skipped,
        report.quarantined,
    )
    return report
