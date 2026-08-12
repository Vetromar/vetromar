"""MeetingMonitor: the notify-then-one-click state machine, driven directly
with fabricated events and clocks (no helper process), plus one supervision
test against a fake helper script."""

from __future__ import annotations

import stat
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

import vetromar.capture.meeting as capture_meeting
from vetromar.errors import ConfigError
from vetromar.ui_server.jobs import JobRegistry
from vetromar.ui_server.meetings import MeetingMonitor

T0 = datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _start(watch="us.zoom.xos", pid=42, name="zoom.us"):
    return {"event": "mic_start", "watch": watch, "bundle_id": watch, "pid": pid, "name": name}


def _stop(watch="us.zoom.xos", pid=42):
    return {"event": "mic_stop", "watch": watch, "pid": pid}


@pytest.fixture
def monitor(monkeypatch):
    m = MeetingMonitor(JobRegistry())
    m._supported = True  # machine-independent tests
    return m


def _fake_record_meeting(out_dir, bundle_prefix, stop_event, on_log=None):
    stop_event.wait(timeout=10)
    out_dir = capture_meeting.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "fake-meeting.wav"
    path.write_bytes(b"RIFF")
    return path


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- detection ---------------------------------------------------------------


def test_mic_start_detects_candidate(monitor):
    monitor.handle_event(_start(), now=T0)
    status = monitor.status()
    assert status["state"] == "detected"
    assert status["candidate"]["name"] == "Zoom"  # watched prefix -> label
    assert status["candidate"]["watch"] == "us.zoom.xos"


def test_mic_stop_clears_candidate_after_debounce(monitor):
    monitor.handle_event(_start(), now=T0)
    monitor.handle_event(_stop(), now=_at(10))
    monitor.check_tick(now=_at(11))  # inside the 3s debounce
    assert monitor.status()["state"] == "detected"
    monitor.check_tick(now=_at(14))
    assert monitor.status()["state"] == "idle"


def test_mic_blip_does_not_flap(monitor):
    monitor.handle_event(_start(), now=T0)
    monitor.handle_event(_stop(), now=_at(10))
    monitor.handle_event(_start(), now=_at(11))  # device-switch blip, same app
    monitor.check_tick(now=_at(20))
    assert monitor.status()["state"] == "detected"


def test_second_app_does_not_replace_candidate(monitor):
    monitor.handle_event(_start(), now=T0)
    monitor.handle_event(_start(watch="com.google.Chrome", name="Chrome"), now=_at(1))
    assert monitor.status()["candidate"]["watch"] == "us.zoom.xos"


def test_unsupported_machine_raises(monkeypatch):
    m = MeetingMonitor(JobRegistry())
    m._supported = False
    with pytest.raises(ConfigError):
        m.start_recording("t", T0)


def test_no_candidate_raises(monitor):
    with pytest.raises(ConfigError):
        monitor.start_recording("t", T0)


# -- recording lifecycle ------------------------------------------------------


def test_record_then_grace_auto_stops(monitor, monkeypatch, tmp_path):
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(capture_meeting, "record_meeting", _fake_record_meeting)

    monitor.handle_event(_start(), now=T0)
    job, started = monitor.start_recording("Standup", T0)
    assert started
    assert monitor.status()["state"] == "recording"
    assert monitor.status()["job_id"] == job.id
    assert _wait_for(lambda: job.status == "recording")

    # The meeting app releases the mic; the grace window opens.
    monitor.handle_event(_stop(), now=_at(60))
    monitor.check_tick(now=_at(70), grace_seconds=20)
    assert not job.stop_event.is_set()  # only 10s in
    # Rejoining within the grace window cancels the stop.
    monitor.handle_event(_start(), now=_at(71))
    monitor.check_tick(now=_at(85), grace_seconds=20)
    assert not job.stop_event.is_set()
    # Gone for good this time.
    monitor.handle_event(_stop(), now=_at(90))
    monitor.check_tick(now=_at(111), grace_seconds=20)
    assert job.stop_event.is_set()
    assert monitor.status()["state"] == "idle"
    assert _wait_for(lambda: job.status == "done")
    assert job.result["audio_path"].endswith("fake-meeting.wav")


def test_single_flight_second_start_attaches(monitor, monkeypatch, tmp_path):
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(capture_meeting, "record_meeting", _fake_record_meeting)

    monitor.handle_event(_start(), now=T0)
    job1, started1 = monitor.start_recording("a", T0)
    job2, started2 = monitor.start_recording("b", T0)
    assert started1 and not started2
    assert job1.id == job2.id
    job1.stop_event.set()
    assert _wait_for(lambda: job1.status == "done")


def test_manual_stop_returns_to_idle(monitor, monkeypatch, tmp_path):
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(capture_meeting, "record_meeting", _fake_record_meeting)

    monitor.handle_event(_start(), now=T0)
    job, _ = monitor.start_recording("t", T0)
    job.stop_event.set()  # what POST /api/record/stop does
    monitor.check_tick(now=_at(5))
    assert monitor.status()["state"] == "idle"


# -- helper supervision (fake helper process) ---------------------------------

FAKE_HELPER = """#!{python}
import json, sys, time
print(json.dumps({{"event": "ready"}}), flush=True)
print(json.dumps({{"event": "mic_start", "watch": "us.zoom.xos",
                   "bundle_id": "us.zoom.xos", "pid": 7, "name": "zoom.us"}}), flush=True)
time.sleep(30)
"""


def test_spawn_helper_feeds_events_into_state_machine(monitor, monkeypatch, tmp_path):
    script = tmp_path / "fake-helper"
    script.write_text(FAKE_HELPER.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(capture_meeting, "find_helper", lambda: script)

    monitor._spawn_helper()
    try:
        assert _wait_for(lambda: monitor.status()["state"] == "detected")
        assert monitor.status()["candidate"]["pid"] == 7
    finally:
        monitor._kill_helper()


def test_respawn_clears_possibly_stale_candidate(monitor, monkeypatch, tmp_path):
    script = tmp_path / "fake-helper"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(capture_meeting, "find_helper", lambda: script)

    monitor.handle_event(_start(), now=T0)
    assert monitor.status()["state"] == "detected"
    monitor._spawn_helper()  # a fresh helper knows nothing about that candidate
    try:
        assert monitor.status()["state"] == "idle"
    finally:
        monitor._kill_helper()
