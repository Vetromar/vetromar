"""Background auto-sync: periodically launch sync jobs for due sources.

One daemon thread, started ONLY by `run_server()` — never by `create_app()`,
so tests and TestClient stay scheduler-free. Every tick re-reads config, so a
Settings change (enable/interval) applies without a restart. Launches go
through `start_sync_job`, meaning auto-syncs are ordinary registry jobs the UI
discovers via GET /api/jobs, and the per-source guard makes overlap with a
manual sync impossible.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from vetromar.config import load_config
from vetromar.store import Store
from vetromar.ui_server.jobs import JobRegistry
from vetromar.ui_server.sync_jobs import start_sync_job

logger = logging.getLogger(__name__)


def _parse_last_synced(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Stored by Store.set_sync_state as aware UTC; treat a naive value as UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SyncScheduler:
    def __init__(
        self,
        registry: JobRegistry,
        *,
        tick_seconds: float = 60.0,
        initial_delay_seconds: float = 20.0,
    ) -> None:
        self._registry = registry
        self._tick_seconds = tick_seconds
        self._initial_delay_seconds = initial_delay_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Retry throttle: a failed/incomplete sync doesn't advance
        # last_synced_at, so without this the source would relaunch every tick.
        self._last_attempt: dict[str, datetime] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
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
                logger.exception("auto-sync tick failed")
            if self._stop.wait(self._tick_seconds):
                return

    def tick(self, now: datetime | None = None) -> list[str]:
        """One scheduling pass; returns the source names launched."""
        from vetromar.sources.registry import load_sources

        now = now or datetime.now(timezone.utc)
        config = load_config()
        from vetromar.ai import ai_available

        if not (
            config.auto_sync_enabled
            and config.backend == "api"
            and ai_available(config)
        ):
            return []
        try:
            sources = [s for s in load_sources() if s.enabled]
        except Exception:  # noqa: BLE001 — a broken sources.toml is not the scheduler's problem
            logger.warning("auto-sync: could not load sources registry", exc_info=True)
            return []
        if not sources:
            return []

        interval = timedelta(minutes=config.auto_sync_interval_minutes)

        # Short-lived Store opened in THIS thread (SQLite is thread-bound);
        # the sync job itself opens its own store in its worker thread.
        config.ensure_dirs()
        last_synced: dict[str, datetime | None] = {}
        store = Store(config.db_path)
        try:
            for source in sources:
                state = store.get_sync_state(source.name)
                last_synced[source.name] = _parse_last_synced(state[1]) if state else None
        finally:
            store.close()

        launched: list[str] = []
        for source in sources:
            last = last_synced[source.name]
            if last is not None and now - last < interval:
                continue
            attempted = self._last_attempt.get(source.name)
            if attempted is not None and now - attempted < interval:
                continue
            job, started = start_sync_job(self._registry, source, initiator="auto")
            self._last_attempt[source.name] = now
            if started:
                logger.info("auto-sync launched for %s (job %s)", source.name, job.id)
                launched.append(source.name)
        return launched
