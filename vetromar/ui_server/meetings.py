"""Meeting detection: notify-then-one-click, never auto-record.

One daemon thread, started ONLY by `run_server()` (the SyncScheduler rule:
create_app()/tests never start background threads). It supervises the native
`vetromar-helper monitor` subprocess, which emits a JSON line whenever a
watched meeting app (Zoom, Teams, a browser) starts or stops using the
microphone. Those events drive a small state machine:

    idle -> detected (candidate shown in tray + notification)
         -> recording (only via an explicit POST /api/meetings/record)
         -> idle (auto-stop: mic released for `meeting_grace_seconds`,
                  or manual POST /api/record/stop)

The state machine (`handle_event` / `check_tick`) is pure logic — tests drive
it directly with fabricated events and clocks; only `_loop` touches processes.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from vetromar.config import load_config
from vetromar.errors import ConfigError
from vetromar.ui_server.jobs import Job, JobRegistry

logger = logging.getLogger(__name__)

# A mic_stop while merely detected must survive short blips (Zoom re-opens
# the mic when switching audio devices) without flapping notifications.
_CANDIDATE_DEBOUNCE_S = 3.0


class MeetingMonitor:
    def __init__(
        self,
        registry: JobRegistry,
        pipeline_tail: Optional[Callable[[Job, Path, str, datetime], dict]] = None,
        *,
        tick_seconds: float = 2.0,
    ) -> None:
        self._registry = registry
        self._pipeline_tail = pipeline_tail
        self._tick_seconds = tick_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._supported: bool | None = None  # cached helper selftest

        # State machine (guarded by _lock).
        self._state = "idle"  # idle | detected | recording
        self._candidate: dict | None = None
        self._candidate_lost_at: datetime | None = None
        self._job: Job | None = None
        self._grace_started: datetime | None = None

    # -- public surface (thread-safe) -----------------------------------------

    def supported(self) -> bool:
        """Whether meeting capture can work here: macOS + helper + selftest.
        Cached — the tray polls status every couple of seconds."""
        if self._supported is None:
            from vetromar.capture.meeting import find_helper, helper_selftest

            if sys.platform != "darwin":
                self._supported = False
            else:
                helper = find_helper()
                self._supported = helper is not None and helper_selftest(helper)
        return self._supported

    def status(self) -> dict:
        with self._lock:
            job = self._job
            return {
                "supported": self.supported(),
                "enabled": load_config().meeting_detect_enabled,
                "state": self._state,
                "candidate": dict(self._candidate) if self._candidate else None,
                "job_id": job.id if job is not None and job.active else None,
            }

    def start_recording(self, title: str, when: datetime) -> tuple[Job, bool]:
        """Begin recording the detected meeting. Single-flight: a second call
        returns the running job. Raises ConfigError when there is nothing to
        record or this machine can't capture system audio."""
        if not self.supported():
            raise ConfigError(
                "Meeting capture isn't available on this machine.",
                hint="It needs the desktop app on macOS 14.2 or newer.",
            )
        with self._lock:
            if self._state == "recording" and self._job is not None and self._job.active:
                return self._job, False
            candidate = self._candidate
            if candidate is None:
                raise ConfigError(
                    "No meeting is currently detected.",
                    hint="Join the meeting first — Vetromar will spot the app "
                    "using the microphone.",
                )

        job, started = self._registry.create_sync_unless_active(
            "meeting",
            {"source": "meeting", "app": candidate.get("name"), "title": title},
            kind="meeting-record",
        )
        if not started:
            return job, False

        bundle_prefix = candidate.get("watch") or candidate.get("bundle_id", "")
        app_name = candidate.get("name", "meeting app")

        def target(run_job: Job) -> dict:
            from vetromar.capture.meeting import record_meeting

            config = load_config()
            config.ensure_dirs()
            run_job.status = "recording"
            run_job.log(f"recording meeting audio from {app_name} + microphone")
            audio = record_meeting(
                config.db_path.parent / "recordings",
                bundle_prefix,
                run_job.stop_event,
                on_log=run_job.log,
            )
            run_job.status = "running"
            if self._pipeline_tail is None:  # direct/test construction only
                return {"audio_path": str(audio)}
            return self._pipeline_tail(run_job, audio, title, when)

        with self._lock:
            self._state = "recording"
            self._job = job
            self._grace_started = None
            self._candidate_lost_at = None
        self._registry.start(job, target)
        return job, True

    # -- state machine (pure logic; tests call these directly) ----------------

    def handle_event(self, event: dict, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        kind = event.get("event")
        if kind not in ("mic_start", "mic_stop"):
            return
        with self._lock:
            if kind == "mic_start":
                self._on_mic_start(event, now)
            else:
                self._on_mic_stop(event, now)

    def _on_mic_start(self, event: dict, now: datetime) -> None:
        watch = event.get("watch")
        if self._state == "recording":
            if self._candidate and watch == self._candidate.get("watch"):
                self._grace_started = None  # rejoined within the grace window
            return
        if self._candidate and watch == self._candidate.get("watch"):
            self._candidate_lost_at = None  # debounced blip, same app
            return
        if self._candidate is None:
            self._candidate = {
                "watch": watch,
                "bundle_id": event.get("bundle_id"),
                "pid": event.get("pid"),
                "name": self._display_name(event),
                "since": now.isoformat(),
            }
            self._candidate_lost_at = None
            self._state = "detected"

    def _on_mic_stop(self, event: dict, now: datetime) -> None:
        watch = event.get("watch")
        if not self._candidate or watch != self._candidate.get("watch"):
            return
        if self._state == "recording":
            self._grace_started = now
        elif self._state == "detected":
            self._candidate_lost_at = now

    def check_tick(self, now: datetime | None = None, grace_seconds: int = 20) -> None:
        """Periodic pass: expire debounced candidates, fire the auto-stop
        grace timer, and fall back to idle when the record job ends."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            if self._state == "detected" and self._candidate_lost_at is not None:
                if (now - self._candidate_lost_at).total_seconds() >= _CANDIDATE_DEBOUNCE_S:
                    self._to_idle()
            elif self._state == "recording":
                job = self._job
                if job is None or not job.active or job.stop_event.is_set():
                    self._to_idle()
                elif (
                    self._grace_started is not None
                    and (now - self._grace_started).total_seconds() >= grace_seconds
                ):
                    job.log(
                        f"meeting app released the microphone {grace_seconds}s ago — "
                        "stopping the recording"
                    )
                    job.stop_event.set()
                    self._to_idle()

    def _to_idle(self) -> None:
        self._state = "idle"
        self._candidate = None
        self._candidate_lost_at = None
        self._grace_started = None
        self._job = None

    @staticmethod
    def _display_name(event: dict) -> str:
        from vetromar.capture.meeting import WATCHED_APPS

        for prefix, label in WATCHED_APPS:
            if event.get("watch") == prefix:
                return label
        return event.get("name") or event.get("bundle_id") or "meeting app"

    # -- helper supervision (real server only) --------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="meeting-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_helper()

    def _loop(self) -> None:
        if self._stop.wait(5.0):  # let the server finish booting first
            return
        while not self._stop.is_set():
            try:
                config = load_config()
                if not (config.meeting_detect_enabled and self.supported()):
                    self._kill_helper()
                elif self._proc is None or self._proc.poll() is not None:
                    self._spawn_helper()
                self.check_tick(grace_seconds=config.meeting_grace_seconds)
            except Exception:  # noqa: BLE001 — the loop must survive any tick
                logger.exception("meeting-monitor tick failed")
            if self._stop.wait(self._tick_seconds):
                return

    def _spawn_helper(self) -> None:
        from vetromar.capture.meeting import find_helper, watched_prefixes

        helper = find_helper()
        if helper is None:
            return
        # Fresh helper = fresh events: a candidate detected before a helper
        # crash may be stale (its mic_stop was never seen); re-detect cleanly.
        with self._lock:
            if self._state == "detected":
                self._to_idle()
        self._proc = subprocess.Popen(
            [str(helper), "monitor", "--bundle-ids", ",".join(watched_prefixes())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )
        logger.info("meeting monitor helper started (pid %s)", self._proc.pid)
        proc = self._proc

        def _read() -> None:
            for line in proc.stdout or []:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.handle_event(event)
            # EOF: the supervision loop notices the dead process and respawns.

        threading.Thread(target=_read, name="meeting-monitor-read", daemon=True).start()

    def _kill_helper(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
