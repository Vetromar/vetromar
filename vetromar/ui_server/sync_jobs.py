"""Shared launcher for sync jobs — one seam for the manual route AND the
background scheduler, so both get the same per-source overlap guard, log tap,
and result shape. The worker opens its own Store (SQLite is thread-bound)."""

from __future__ import annotations

import logging

from vetromar.config import load_config
from vetromar.sources.registry import SourceConfig
from vetromar.store import Store
from vetromar.ui_server.jobs import Job, JobRegistry

logger = logging.getLogger(__name__)


class _JobLogHandler(logging.Handler):
    """Tap a logger's records into job.progress so the UI can watch the sync
    agent work (per-tool-call lines) without sync_source growing a callback."""

    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self._job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._job.log(record.getMessage())
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass


def start_sync_job(
    registry: JobRegistry,
    source: SourceConfig,
    *,
    full: bool = False,
    dry_run: bool = False,
    extract: bool = True,
    initiator: str = "manual",  # "manual" | "auto"
) -> tuple[Job, bool]:
    """Launch a sync job for `source`, unless one is already queued/running for
    it — then return (existing_job, False) so the caller attaches instead of
    double-syncing (the cursor read-modify-write is not concurrency-safe)."""
    job, started = registry.create_sync_unless_active(
        source.name, {"source": source.name, "initiator": initiator}
    )
    if not started:
        return job, False

    def target(job: Job) -> dict:
        from vetromar.sources.sync import sync_source

        config = load_config()
        config.ensure_dirs()
        job.set_stage("Syncing — the agent is pulling from the source…", None)
        sync_logger = logging.getLogger("vetromar.sources.sync")
        handler = _JobLogHandler(job)
        old_level = sync_logger.level
        sync_logger.addHandler(handler)
        sync_logger.setLevel(logging.INFO)
        store = Store(config.db_path)
        try:
            report = sync_source(
                store,
                source,
                config,
                full=full,
                dry_run=dry_run,
                extract=extract,
            )
        finally:
            sync_logger.removeHandler(handler)
            sync_logger.setLevel(old_level)
            store.close()
        job.set_stage("Done", None)
        return {
            "source": report.source,
            "created": report.created,
            "duplicates": report.duplicates,
            "units": report.units,
            "extraction_failures": report.extraction_failures,
            "cursor": report.cursor,
            "dry_run": report.dry_run,
            "incomplete": report.incomplete,
        }

    registry.start(job, target)
    return job, True
