"""Background workspace sync: keep this device converged with the workspace.

The SyncScheduler pattern: one daemon thread, started ONLY by `run_server()`
(never `create_app()` — tests stay scheduler-free); every tick re-reads config
and the session cache, so sign-in/out applies without a restart. Runs iff a
cloud session exists; launches go through
`start_workspace_sync_job`, so overlap with a manual Sync-now is impossible.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from vetromar.config import load_config
from vetromar.ui_server.jobs import JobRegistry
from vetromar.ui_server.workspace_jobs import start_workspace_sync_job

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
        # A failed sync doesn't advance last_synced_at — throttle retries.
        self._last_attempt: datetime | None = None

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
                logger.exception("workspace-sync tick failed")
            if self._stop.wait(self._tick_seconds):
                return

    def tick(self, now: datetime | None = None) -> bool:
        """One pass; returns whether a sync was launched."""
        from vetromar.workspace import auth as ws_auth

        now = now or datetime.now(timezone.utc)
        config = load_config()
        if not config.cloud_token:
            return False
        status = ws_auth.status()
        if not status["signed_in"]:
            return False

        interval = timedelta(minutes=max(1, config.workspace_sync_interval_minutes))
        last = _parse_iso(status.get("last_synced_at"))
        if last is not None and now - last < interval:
            return False
        if self._last_attempt is not None and now - self._last_attempt < interval:
            return False

        job, started = start_workspace_sync_job(self._registry, initiator="auto")
        self._last_attempt = now
        if started:
            logger.info("workspace auto-sync launched (job %s)", job.id)
        return started
