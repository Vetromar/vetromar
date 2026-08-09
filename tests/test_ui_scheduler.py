"""SyncScheduler.tick() decision logic — called directly, never start()ed
(the thread only ever runs inside a real `vetromar ui-server`; create_app and
these tests stay scheduler-free by construction)."""

from datetime import datetime, timedelta, timezone

import pytest

import vetromar.ui_server.scheduler as scheduler_mod
from vetromar.config import save_config
from vetromar.store import Store
from vetromar.ui_server.jobs import JobRegistry
from vetromar.ui_server.scheduler import SyncScheduler


@pytest.fixture
def env(tmp_path, monkeypatch):
    import vetromar.config as config_mod
    import vetromar.sources.registry as registry_mod

    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_INTERVAL_MINUTES", raising=False)
    monkeypatch.setattr(config_mod, "CREDENTIALS_PATH", tmp_path / "credentials")
    monkeypatch.setattr(registry_mod, "VETROMAR_HOME", tmp_path)
    return tmp_path


@pytest.fixture
def launches(monkeypatch):
    """Stub start_sync_job in the scheduler's namespace, recording launches.
    The stub's job finishes immediately so later ticks see no active sync."""
    calls: list[tuple[str, str]] = []

    def fake_start(registry, source, **kw):
        calls.append((source.name, kw.get("initiator")))
        job = registry.create("sync", meta={"source": source.name})
        job.status = "done"
        return job, True

    monkeypatch.setattr(scheduler_mod, "start_sync_job", fake_start)
    return calls


def _add_source(name="notion"):
    from vetromar.sources.registry import SourceConfig, upsert_source

    upsert_source(SourceConfig(name=name, url="https://mcp.notion.com/mcp"))


def test_disabled_or_unready_backend_skips_cleanly(env, launches, monkeypatch):
    _add_source()
    sched = SyncScheduler(JobRegistry())
    # auto-sync disabled (the default)
    assert sched.tick() == []
    save_config({"auto_sync_enabled": True})
    # local backend
    monkeypatch.setenv("VETROMAR_BACKEND", "local")
    assert sched.tick() == []
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    # no API key
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert sched.tick() == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # everything ready -> the never-synced source launches
    assert sched.tick() == ["notion"]
    assert launches == [("notion", "auto")]


def test_workspace_token_alone_does_not_launch(env, launches, monkeypatch, tmp_path):
    # Post-pivot a workspace session is sync-only: without a configured AI
    # provider the scheduler must not launch source syncs.
    _add_source()
    save_config({"auto_sync_enabled": True})
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    creds = tmp_path / "credentials-cloud"
    creds.write_text("tok_x\n")
    monkeypatch.setenv("VETROMAR_CLOUD_CREDENTIALS", str(creds))
    assert SyncScheduler(JobRegistry()).tick() == []
    assert launches == []


def test_no_sources_is_a_clean_noop(env, launches):
    save_config({"auto_sync_enabled": True})
    assert SyncScheduler(JobRegistry()).tick() == []
    assert launches == []


def test_fresh_source_not_due_stale_source_due(env, launches):
    from vetromar.config import load_config

    _add_source()
    save_config({"auto_sync_enabled": True, "auto_sync_interval_minutes": 30})
    config = load_config()
    config.ensure_dirs()
    store = Store(config.db_path)
    store.set_sync_state("notion", "{}")  # last_synced_at = now
    store.close()

    sched = SyncScheduler(JobRegistry())
    now = datetime.now(timezone.utc)
    assert sched.tick(now=now + timedelta(minutes=5)) == []
    assert sched.tick(now=now + timedelta(minutes=45)) == ["notion"]


def test_active_sync_for_source_blocks_relaunch(env):
    # Real start_sync_job path: a queued/running sync (e.g. a manual one from
    # the UI) means tick() attaches instead of launching a second sync.
    _add_source()
    save_config({"auto_sync_enabled": True})
    registry = JobRegistry()
    registry.create_sync_unless_active("notion", {"source": "notion", "initiator": "manual"})
    assert SyncScheduler(registry).tick() == []
    assert len(registry.list(kind="sync")) == 1


def test_attempt_throttle_prevents_hot_loop(env, launches):
    # A failed/incomplete sync never advances last_synced_at — without the
    # throttle the source would relaunch on every tick.
    _add_source()
    save_config({"auto_sync_enabled": True, "auto_sync_interval_minutes": 30})
    sched = SyncScheduler(JobRegistry())
    now = datetime.now(timezone.utc)
    assert sched.tick(now=now) == ["notion"]
    assert sched.tick(now=now + timedelta(minutes=1)) == []
    assert sched.tick(now=now + timedelta(minutes=31)) == ["notion"]
    assert launches == [("notion", "auto"), ("notion", "auto")]
