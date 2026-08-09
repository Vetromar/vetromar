"""In-process job registry for the desktop UI API.

Transcription + extraction take minutes, so the capture/record/provision
endpoints can't run inline in a request. Each becomes a `Job` run on a daemon
thread; the frontend polls `GET /api/jobs/{id}`. Deliberately dependency-light —
no Celery/Redis; this is a single local process serving one desktop app.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from vetromar.errors import present_error

logger = logging.getLogger("vetromar.ui_server.jobs")


@dataclass
class Job:
    id: str
    kind: str  # "capture" | "record" | "download-models" | "connect" | "test-source" | "sync"
    status: str = "queued"  # queued | recording | running | done | error
    progress: list[str] = field(default_factory=list)
    stage: Optional[str] = None  # current pipeline stage label
    percent: Optional[float] = None  # percent within the current stage, if known
    result: Optional[dict] = None
    error: Optional[str] = None
    error_code: Optional[str] = None  # VM-* support code; None for expected errors
    # Kind-specific context, e.g. {"source": ..., "initiator": "auto"} on sync
    # jobs so the UI can discover scheduler-launched runs.
    meta: dict = field(default_factory=dict)
    # Record jobs record until this is set (by POST /api/record/stop).
    stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None

    def log(self, message: str) -> None:
        self.progress.append(message)

    def set_stage(self, stage: str, percent: Optional[float]) -> None:
        self.stage = stage
        self.percent = percent

    def public(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": list(self.progress),
            "stage": self.stage,
            "percent": self.percent,
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
            "meta": dict(self.meta),
        }

    @property
    def active(self) -> bool:
        return self.status not in ("done", "error")


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, meta: Optional[dict] = None) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind, meta=meta or {})
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, kind: Optional[str] = None, active_only: bool = False) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if kind is not None:
            jobs = [j for j in jobs if j.kind == kind]
        if active_only:
            jobs = [j for j in jobs if j.active]
        return jobs

    def create_sync_unless_active(
        self, source: str, meta: dict, kind: str = "sync"
    ) -> tuple[Job, bool]:
        """Atomically return the active `kind` job for `source`, or create a
        new one. The per-key guard: sync cursors are read-modify-write and not
        concurrency-safe, so manual + auto runs must never overlap. Used by
        source syncs (kind='sync', key=source name) and workspace syncs
        (kind='workspace-sync', one global key)."""
        with self._lock:
            for existing in self._jobs.values():
                if (
                    existing.kind == kind
                    and existing.meta.get("source") == source
                    and existing.active
                ):
                    return existing, False
            job = Job(id=uuid.uuid4().hex, kind=kind, meta=meta)
            self._jobs[job.id] = job
            return job, True

    def start(self, job: Job, target: Callable[[Job], dict]) -> None:
        """Run `target(job)` on a daemon thread, recording result or error.

        `target` returns the job's result dict; any exception becomes
        `status='error'` — expected errors (ConfigError, workspace sign-in)
        surface their own message, everything else shows a generic message +
        VM-* code while the full traceback goes to the sidecar log."""

        def _worker() -> None:
            job.status = "running"
            try:
                job.result = target(job)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 — reported to the UI as job error
                job.error, job.error_code = present_error(exc)
                if job.error_code is None:
                    logger.warning("job %s (%s) failed: %s", job.id, job.kind, exc)
                else:
                    logger.exception(
                        "job %s (%s) failed [%s]", job.id, job.kind, job.error_code
                    )
                job.status = "error"

        thread = threading.Thread(target=_worker, name=f"job-{job.id}", daemon=True)
        job._thread = thread
        thread.start()
