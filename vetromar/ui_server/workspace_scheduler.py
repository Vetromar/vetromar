"""Background graph sync: keep this device converged with every shared graph
it belongs to.

The SyncScheduler pattern: one daemon thread, started ONLY by `run_server()`
(never `create_app()` — tests stay scheduler-free); every tick re-reads
config and the graph registry, so joins/leaves apply without a restart. Each
connected graph is considered independently; launches go through
`start_graph_sync_job`, whose per-graph guard makes overlap with a manual
Sync-now impossible.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from vetromar import graphs
from vetromar.config import load_config
from vetromar.ui_server.jobs import JobRegistry
from vetromar.ui_server.workspace_jobs import start_graph_sync_job

logger = logging.getLogger(__name__)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class WorkspaceSyncScheduler:
    def __init__(
        self,
        registry: JobRegistry,
        *,
        tick_seconds: float = 30.0,
        initial_delay_seconds: float = 15.0,
    ) -> None:
        self._registry = registry
        self._tick_seconds = tick_seconds
        self._initial_delay_seconds = initial_delay_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # A failed sync doesn't advance last_synced_at — throttle retries,
        # independently per graph.
        self._last_attempt: dict[str, datetime] = {}

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="workspace-sync-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(self._initial_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive any tick
                logger.exception("graph-sync tick failed")
            if self._stop.wait(self._tick_seconds):
                return

    def tick(self, now: datetime | None = None) -> int:
        """One pass; returns how many syncs were launched."""
        now = now or datetime.now(timezone.utc)
        config = load_config()
        interval = timedelta(minutes=max(1, config.workspace_sync_interval_minutes))

        launched = 0
        for info in graphs.list_graphs():
            if not info.synced:
                continue
            last = _parse_iso(info.last_synced_at)
            if last is not None and now - last < interval:
                continue
            attempt = self._last_attempt.get(info.id)
            if attempt is not None and now - attempt < interval:
                continue
            job, started = start_graph_sync_job(
                self._registry, info.id, initiator="auto"
            )
            self._last_attempt[info.id] = now
            if started:
                launched += 1
                logger.info(
                    "graph auto-sync launched for %s (job %s)", info.id, job.id
                )
        return launched
