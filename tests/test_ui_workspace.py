"""Desktop UI graph-connection routes — identity, per-graph sync, scheduler.

The graph host runs in-process (real `cloud.app` over TestClient, injected
at the CloudClient seam), so these are true end-to-end flows minus the socket.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cloud.app import create_app as cloud_create_app
from tests.cloud_helpers import create_graph as cloud_create_graph
from vetromar import graphs
from vetromar.ui_server.app import create_app


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    import vetromar.config as config_mod

    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("VETROMAR_IDENTITY_KEY", str(tmp_path / "identity.key"))
    monkeypatch.setattr(config_mod, "CREDENTIALS_PATH", tmp_path / "credentials")
    monkeypatch.setattr(
        config_mod, "DEEPGRAM_CREDENTIALS_PATH", tmp_path / "credentials-deepgram"
    )
    return tmp_path


@pytest.fixture
def cloud_backend(isolated_env, monkeypatch):
    """A real in-process graph host, injected at the CloudClient transport
    seam so every ui_server route exercises the genuine auth/sync code."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    cloud_app = cloud_create_app(engine=engine)

    from vetromar.workspace import client as client_mod

    real_init = client_mod.CloudClient.__init__

    def patched_init(self, base_url, token=None, workspace_id=None, http=None):
        real_init(
            self,
            base_url,
            token=token,
            workspace_id=workspace_id,
            http=http or TestClient(cloud_app),
        )

    monkeypatch.setattr(client_mod.CloudClient, "__init__", patched_init)
    return {"app": cloud_app, "engine": engine, "http": TestClient(cloud_app)}


@pytest.fixture
def client(isolated_env):
    return TestClient(create_app())


def _wait_job(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {last}")


def _connected_graph(client, cloud_backend, name="Crew"):
    """Create a local graph + a workspace hosted by THIS machine's identity
    (enrolled as the in-process server's owner), and wire them together."""
    from vetromar.identity import ensure_identity

    identity = ensure_identity()
    ws = cloud_create_graph(
        cloud_backend["http"], cloud_backend["engine"], identity, name=name
    )
    info = client.post("/api/graphs", json={"name": name}).json()
    graphs.update_graph(
        info["id"],
        host_url="http://testserver",
        workspace_id=ws["workspace_id"],
        role="host",
        handle="host",
    )
    # Connect flows bind the store at wire-up time (it's empty here, so this
    # is the silent first-bind, not an upload decision).
    from vetromar.store import Store
    from vetromar.workspace.engine import bind_workspace

    store = Store(graphs.resolve_db_path(info["id"]))
    bind_workspace(store, ws["workspace_id"])
    store.close()
    return info["id"], ws["workspace_id"]


# -- identity ---------------------------------------------------------------------


def test_identity_generated_once_and_0600(client, isolated_env):
    first = client.get("/api/identity").json()
    assert first["public_key"]
    assert client.get("/api/identity").json()["public_key"] == first["public_key"]
    key_file = isolated_env / "identity.key"
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600


# -- per-graph sync ------------------------------------------------------------------


def test_sync_unknown_graph_404_and_unconnected_400(client):
    assert client.post("/api/graphs/g_nope/sync").status_code == 404
    local = client.post("/api/graphs", json={"name": "Loose"}).json()
    resp = client.post(f"/api/graphs/{local['id']}/sync")
    assert resp.status_code == 400
    assert "not connected" in resp.json()["detail"]


def test_graph_sync_end_to_end(client, cloud_backend):
    graph_id, _ = _connected_graph(client, cloud_backend)
    # A note lands in the graph, sync pushes it to the host.
    client.post(f"/api/graphs/{graph_id}/note", json={"text": "brass hinge"})
    resp = client.post(f"/api/graphs/{graph_id}/sync")
    assert resp.status_code == 200
    job = _wait_job(client, resp.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["pushed"] >= 2  # episode + unit
    assert job["meta"]["graph"] == graph_id
    # The registry records the sync time; the store is bound.
    info = graphs.get_graph(graph_id)
    assert info.last_synced_at is not None


def test_second_sync_is_noop_and_guard_is_per_graph(client, cloud_backend):
    g1, _ = _connected_graph(client, cloud_backend, name="One")
    g2, _ = _connected_graph(client, cloud_backend, name="Two")
    r1 = client.post(f"/api/graphs/{g1}/sync")
    r2 = client.post(f"/api/graphs/{g2}/sync")
    # Different graphs never share a guard: both launched fresh.
    assert r1.json()["already_running"] is False
    assert r2.json()["already_running"] is False
    j1 = _wait_job(client, r1.json()["job_id"])
    j2 = _wait_job(client, r2.json()["job_id"])
    assert j1["status"] == j2["status"] == "done"
    r1b = client.post(f"/api/graphs/{g1}/sync")
    assert _wait_job(client, r1b.json()["job_id"])["result"]["pulled"] == 0


# -- scheduler ----------------------------------------------------------------------


def test_scheduler_ticks_per_graph(client, cloud_backend):
    from vetromar.ui_server.jobs import JobRegistry
    from vetromar.ui_server.workspace_scheduler import WorkspaceSyncScheduler

    g1, _ = _connected_graph(client, cloud_backend, name="One")
    g2, _ = _connected_graph(client, cloud_backend, name="Two")
    client.post("/api/graphs", json={"name": "Local only"})  # never synced

    registry = JobRegistry()
    scheduler = WorkspaceSyncScheduler(registry)
    assert scheduler.tick() == 2  # one per CONNECTED graph, nothing else
    assert scheduler.tick() == 0  # throttled within the interval

    deadline = time.time() + 10
    while time.time() < deadline and any(j.active for j in registry.list()):
        time.sleep(0.02)
    jobs = registry.list(kind="workspace-sync")
    assert {j.meta["graph"] for j in jobs} == {g1, g2}
    assert all(j.status == "done" for j in jobs), [(j.meta, j.error) for j in jobs]
