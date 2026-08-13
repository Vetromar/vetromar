"""Host mode end-to-end: the embedded graph host over a REAL socket, the
join flow, and member management through the ui_server routes."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tests.cloud_helpers import new_identity
from vetromar import graphs
from vetromar.ui_server.app import create_app
from vetromar.workspace.client import CloudClient
from vetromar.workspace.engine import sync_workspace


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
def client(isolated_env):
    return TestClient(create_app())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(url: str, timeout=10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url + "/v1/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise AssertionError(f"host at {url} never became healthy")


def _wait_job(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {last}")


@pytest.fixture
def embedded_host(isolated_env, monkeypatch):
    """This machine hosting: the real HostServer on a real port, with the
    host db under the isolated home."""
    from vetromar.hosting import server as hosting_server

    monkeypatch.setattr(
        hosting_server, "HOST_DB_PATH", isolated_env / "host" / "cloud.db"
    )
    host = hosting_server.HostServer()
    monkeypatch.setattr(hosting_server, "HOST", host)
    port = _free_port()
    monkeypatch.setenv("VETROMAR_HOST_ENABLED", "1")
    monkeypatch.setenv("VETROMAR_HOST_PORT", str(port))
    host.start(port, "127.0.0.1")
    _wait_health(f"http://127.0.0.1:{port}")
    yield {"port": port, "url": f"http://127.0.0.1:{port}"}
    host.stop()


def test_host_mode_end_to_end(client, embedded_host, tmp_path):
    """Create a hosted graph via the UI API, invite a friend, and prove the
    friend's device converges over the real socket."""
    status = client.get("/api/host").json()
    assert status["enabled"] and status["running"]
    assert status["port"] == embedded_host["port"]

    created = client.post(
        "/api/host/graphs", json={"name": "Crew", "handle": "leo"}
    ).json()
    assert created["role"] == "host"
    assert created["host_url"] == embedded_host["url"]

    # A note on the host side, synced up.
    client.post(f"/api/graphs/{created['id']}/note", json={"text": "brass hinge"})
    job = _wait_job(client, client.post(f"/api/graphs/{created['id']}/sync").json()["job_id"])
    assert job["status"] == "done", job

    # Invite minted through the graph-scoped route.
    invite = client.post(f"/api/graphs/{created['id']}/invites", json={}).json()
    assert invite["url"].startswith(embedded_host["url"] + "/invite-accept?token=")

    # The friend: their own identity, their own store, real HTTP.
    friend = new_identity()
    friend_client = CloudClient(embedded_host["url"])
    joined = friend_client.accept_invite(invite["url"].split("token=")[1], friend, "mo", "Mo")
    friend_client.workspace_id = joined["workspace_id"]

    from vetromar.store import Store

    friend_store = Store(tmp_path / "friend-store.db")
    report = sync_workspace(
        friend_store, friend_client, "dev-friend", workspace_id=joined["workspace_id"]
    )
    assert report.applied >= 2
    assert friend_store.search_fts("brass hinge")
    # The friend contributes back; the host pulls it.
    from vetromar.ingest.notes import add_quick_note

    add_quick_note(friend_store, "steel was wrong for the salt air")
    sync_workspace(
        friend_store, friend_client, "dev-friend", workspace_id=joined["workspace_id"]
    )
    job = _wait_job(client, client.post(f"/api/graphs/{created['id']}/sync").json()["job_id"])
    assert job["status"] == "done"
    hits = client.get(
        "/api/store/search", params={"graph": created["id"], "text": "salt air"}
    ).json()
    assert hits, "friend's note never reached the host"

    # Members panel: both present; role change is host-only and works.
    members = client.get(f"/api/graphs/{created['id']}/members").json()["members"]
    assert {m["handle"] for m in members} == {"leo", "mo"}
    mo = next(m for m in members if m["handle"] == "mo")
    resp = client.post(
        f"/api/graphs/{created['id']}/members/{mo['principal_id']}/role",
        json={"role": "admin"},
    )
    assert resp.status_code == 200 and resp.json()["role"] == "admin"
    assert (
        client.delete(
            f"/api/graphs/{created['id']}/members/{mo['principal_id']}"
        ).json()
        == {"ok": True}
    )
    friend_store.close()
    friend_client.close()


def test_hosting_requires_enabled(client):
    resp = client.post("/api/host/graphs", json={"name": "Nope"})
    assert resp.status_code == 400
    assert "hosting on" in resp.json()["detail"]


def test_join_route_against_remote_host(client, isolated_env, tmp_path):
    """The member side: paste an invite link from someone ELSE's server and
    the app enrolls, replicates, and syncs."""
    import uvicorn

    from cloud.__main__ import set_owner
    from cloud.app import create_app as cloud_create_app
    from cloud.db import make_engine

    host_identity = new_identity()  # the FRIEND's identity, not this machine's
    engine = make_engine("sqlite:///" + str(tmp_path / "remote-cloud.db"))
    set_owner(engine, host_identity.public_key)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            cloud_create_app(engine=engine), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    _wait_health(url)
    try:
        friend_client = CloudClient(url)
        friend_client.login_with_key(host_identity)
        ws = friend_client.create_workspace("Mo's world", "mo", "Mo")
        friend_client.workspace_id = ws["workspace_id"]
        invite = friend_client.create_invite()
        friend_client.close()

        resp = client.post(
            "/api/graphs/join",
            json={"invite_url": f"{url}{invite['url_path']}", "handle": "leo"},
        )
        assert resp.status_code == 200, resp.text
        joined = resp.json()
        assert joined["name"] == "Mo's world"
        assert joined["role"] == "member"
        assert joined["host_url"] == url
        job = _wait_job(client, joined["sync_job_id"])
        assert job["status"] == "done", job
        info = graphs.get_graph(joined["id"])
        assert info.last_synced_at is not None

        # Garbage links are rejected up front.
        bad = client.post(
            "/api/graphs/join", json={"invite_url": "not a url", "handle": "x"}
        )
        assert bad.status_code == 400
    finally:
        server.should_exit = True
        thread.join(timeout=5)
