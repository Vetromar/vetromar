"""Desktop UI workspace routes — sign-in gate, team management, sync jobs.

The cloud service runs in-process (real `cloud.app` over TestClient, injected
at the CloudClient seam), so these are true end-to-end flows minus the socket.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cloud.app import create_app as cloud_create_app
from cloud.db import make_sessionmaker
from cloud.models import Workspace, utcnow
from vetromar.ui_server.app import create_app


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    import vetromar.config as config_mod

    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("VETROMAR_CLOUD_CREDENTIALS", str(tmp_path / "credentials-cloud"))
    monkeypatch.setenv("VETROMAR_WORKSPACE_CACHE", str(tmp_path / "workspace.json"))
    monkeypatch.setenv("VETROMAR_WEBSITE_URL", "https://vetromar.test")
    monkeypatch.setenv("VETROMAR_CLOUD_API_URL", "https://cloud.vetromar.test")
    monkeypatch.delenv("VETROMAR_CLOUD_TOKEN", raising=False)
    monkeypatch.setattr(config_mod, "CREDENTIALS_PATH", tmp_path / "credentials")
    monkeypatch.setattr(
        config_mod, "DEEPGRAM_CREDENTIALS_PATH", tmp_path / "credentials-deepgram"
    )
    return tmp_path


@pytest.fixture
def cloud_backend(isolated_env, monkeypatch):
    """A real in-process cloud service, injected at the CloudClient transport
    seam so every ui_server route exercises the genuine account/sync code."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    cloud_app = cloud_create_app(engine=engine)

    from vetromar.workspace import client as client_mod

    real_init = client_mod.CloudClient.__init__

    def patched_init(self, base_url, token=None, http=None):
        real_init(self, base_url, token=token, http=http or TestClient(cloud_app))

    monkeypatch.setattr(client_mod.CloudClient, "__init__", patched_init)
    return {"app": cloud_app, "engine": engine, "http": TestClient(cloud_app)}


@pytest.fixture
def client(cloud_backend):
    return TestClient(create_app())


def make_workspace(
    cloud_backend,
    email="ada@acme.test",
    password="hunter22hunter22",
):
    resp = cloud_backend["http"].post(
        "/v1/workspaces",
        json={
            "workspace_name": "Acme",
            "name": "Ada",
            "email": email,
            "password": password,
        },
    )
    assert resp.status_code == 201
    return resp.json()


def sign_in(client, email="ada@acme.test", password="hunter22hunter22"):
    resp = client.post(
        "/api/workspace/signin", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_signed_out_status(client):
    body = client.get("/api/workspace").json()
    assert body["signed_in"] is False
    assert body["server_url"].startswith("http")


def test_signin_persists_token_0600_and_session(client, cloud_backend, isolated_env):
    make_workspace(cloud_backend)
    status = sign_in(client)
    assert status["signed_in"] is True
    assert status["workspace"] == "Acme"
    assert status["role"] == "admin"
    token_file = isolated_env / "credentials-cloud"
    assert token_file.exists()
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    body = client.get("/api/workspace").json()
    assert body["signed_in"] is True
    assert body["quarantine_count"] == 0
    assert body["device_id"].startswith("dev_")


def test_bad_credentials_are_a_400(client, cloud_backend):
    make_workspace(cloud_backend)
    resp = client.post(
        "/api/workspace/signin", json={"email": "ada@acme.test", "password": "nope-nope"}
    )
    assert resp.status_code == 400
    assert "invalid" in resp.json()["detail"]


def test_signout_clears_session(client, cloud_backend, isolated_env):
    make_workspace(cloud_backend)
    sign_in(client)
    assert client.post("/api/workspace/signout").json() == {"ok": True}
    assert not (isolated_env / "credentials-cloud").exists()
    assert client.get("/api/workspace").json()["signed_in"] is False
    # Team routes require a session again.
    assert client.get("/api/workspace/members").status_code == 401


def test_invite_url_composition(client, cloud_backend):
    make_workspace(cloud_backend)
    sign_in(client)
    invite = client.post("/api/workspace/invites", json={}).json()
    assert invite["url"].startswith("https://cloud.vetromar.test/invite-accept?token=")
    assert invite["token"] in invite["url"]


def test_member_reset_link_via_ui(client, cloud_backend):
    make_workspace(cloud_backend)
    sign_in(client)
    invite = client.post("/api/workspace/invites", json={}).json()
    cloud_backend["http"].post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "Mo",
            "email": "mo@acme.test",
            "password": "memberpass123",
        },
    )
    members = client.get("/api/workspace/members").json()["members"]
    mo = next(m for m in members if m["email"] == "mo@acme.test")

    r = client.post(f"/api/workspace/members/{mo['user_id']}/reset-link")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"].startswith("https://cloud.vetromar.test/reset-password?token=")
    assert body["name"] == "Mo"

    # The minted link really resets the password on the server.
    confirm = cloud_backend["http"].post(
        "/v1/auth/reset-confirm",
        json={"token": body["token"], "password": "newpassword99"},
    )
    assert confirm.status_code == 200, confirm.text
    login = cloud_backend["http"].post(
        "/v1/auth/login", json={"email": "mo@acme.test", "password": "newpassword99"}
    )
    assert login.status_code == 200


def test_members_flow_through_ui(client, cloud_backend):
    make_workspace(cloud_backend)
    sign_in(client)
    invite = client.post("/api/workspace/invites", json={}).json()
    cloud_backend["http"].post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "Mo",
            "email": "mo@acme.test",
            "password": "memberpass123",
        },
    )
    members = client.get("/api/workspace/members").json()["members"]
    assert {m["email"] for m in members} == {"ada@acme.test", "mo@acme.test"}
    mo = next(m for m in members if m["email"] == "mo@acme.test")
    assert client.delete(f"/api/workspace/members/{mo['user_id']}").json() == {"ok": True}
    members = client.get("/api/workspace/members").json()["members"]
    assert [m for m in members if m["email"] == "mo@acme.test"][0]["active"] is False


def _wait(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {last}")


def test_workspace_sync_job_lifecycle(client, cloud_backend):
    make_workspace(cloud_backend)
    sign_in(client)
    resp = client.post("/api/workspace/sync").json()
    job = _wait(client, resp["job_id"])
    assert job["status"] == "done", job
    assert job["kind"] == "workspace-sync"
    assert job["result"]["pushed"] == 0
    # A second sync is another clean no-op.
    job2 = _wait(client, client.post("/api/workspace/sync").json()["job_id"])
    assert job2["status"] == "done"


def test_sync_requires_signin(client):
    assert client.post("/api/workspace/sync").status_code == 401


def test_double_sync_attaches_to_running_job(client, cloud_backend, monkeypatch):
    make_workspace(cloud_backend)
    sign_in(client)
    release = threading.Event()

    import vetromar.workspace.engine as engine_mod

    real_sync = engine_mod.sync_workspace

    def slow_sync(store, cloud_client, device_id, workspace_id=None):
        release.wait(timeout=5)
        return real_sync(store, cloud_client, device_id, workspace_id=workspace_id)

    monkeypatch.setattr(engine_mod, "sync_workspace", slow_sync)
    first = client.post("/api/workspace/sync").json()
    second = client.post("/api/workspace/sync").json()
    assert second["job_id"] == first["job_id"]
    assert second["already_running"] is True
    release.set()
    assert _wait(client, first["job_id"])["status"] == "done"


def test_scheduler_tick_gates_and_launches(client, cloud_backend):
    from vetromar.ui_server.jobs import JobRegistry
    from vetromar.ui_server.workspace_scheduler import WorkspaceSyncScheduler
    from vetromar.workspace import auth as ws_auth

    registry = JobRegistry()
    scheduler = WorkspaceSyncScheduler(registry)
    # Signed out: nothing runs.
    assert scheduler.tick() is False

    make_workspace(cloud_backend)
    sign_in(client)
    # Signed in, never synced: launches.
    assert scheduler.tick() is True
    job = registry.list(kind="workspace-sync")[0]
    _wait(client_for_registry(registry), job.id)
    # Throttled within the interval (last_attempt + fresh last_synced_at).
    assert scheduler.tick() is False


def client_for_registry(registry):
    """Poll jobs straight off the registry (scheduler jobs aren't in the app's
    module-level _JOBS)."""

    class _Shim:
        def get(self, path):
            job = registry.get(path.rsplit("/", 1)[1])

            class R:
                @staticmethod
                def json():
                    return job.public()

            return R()

    return _Shim()


# -- deletion + binding (M22) ------------------------------------------------


def test_delete_workspace_via_ui(client, cloud_backend):
    make_workspace(cloud_backend)
    sign_in(client)

    wrong = client.post("/api/workspace/delete", json={"password": "not-it-at-all"})
    assert wrong.status_code == 400
    assert "password" in wrong.json()["detail"]
    assert client.get("/api/workspace").json()["signed_in"] is True

    resp = client.post(
        "/api/workspace/delete", json={"password": "hunter22hunter22"}
    )
    assert resp.status_code == 200 and resp.json() == {"deleted": True}
    # Local session cleared…
    assert client.get("/api/workspace").json()["signed_in"] is False
    # …and the account is truly gone server-side.
    login = cloud_backend["http"].post(
        "/v1/auth/login",
        json={"email": "ada@acme.test", "password": "hunter22hunter22"},
    )
    assert login.status_code == 401


def test_delete_account_via_ui_solo_user(client, cloud_backend):
    make_workspace(cloud_backend)
    sign_in(client)
    resp = client.post("/api/account/delete", json={"password": "hunter22hunter22"})
    assert resp.status_code == 200 and resp.json() == {"deleted": True}
    assert client.get("/api/workspace").json()["signed_in"] is False


def test_binding_status_and_upload_decision(client, cloud_backend, isolated_env):
    import os

    from vetromar.store import Store

    make_workspace(cloud_backend)
    sign_in(client)

    # Fresh store: nothing ever pushed → no decision needed.
    assert client.get("/api/workspace/binding").json()["status"] == "ok"

    # Simulate a store that synced with a previous (now deleted) workspace:
    # a pushed outbox row and no binding.
    store = Store(os.environ["VETROMAR_DB"])
    store.record_change("episodes", "insert", "ep_old", "{}")
    store._conn.execute("UPDATE changelog SET pushed = 1")
    store._conn.commit()
    store.close()
    assert client.get("/api/workspace/binding").json()["status"] == "needs_decision"

    # Sync refuses until the human decides.
    job = client.post("/api/workspace/sync").json()
    finished = _wait(client, job["job_id"])
    assert finished["status"] == "error"
    assert "different workspace" in finished["error"]

    bad = client.post("/api/workspace/binding", json={"action": "nonsense"})
    assert bad.status_code == 400

    decided = client.post("/api/workspace/binding", json={"action": "upload"}).json()
    assert decided == {"ok": True, "requeued": 1}
    assert client.get("/api/workspace/binding").json()["status"] == "ok"

def test_solo_store_asks_before_first_upload(client, cloud_backend, isolated_env):
    """Solo-era knowledge (captured with no account) must not bulk-upload on
    first connect — the banner decision gates the sync."""
    import os
    from datetime import datetime, timezone

    from vetromar.ingest.manual import add_source_episode
    from vetromar.store import Store

    # Capture BEFORE any account exists.
    store = Store(os.environ["VETROMAR_DB"])
    add_source_episode(
        store,
        title="Solo thoughts",
        source_kind="note",
        raw="Captured before any workspace existed.",
        occurred_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
    )
    store.close()

    make_workspace(cloud_backend)
    sign_in(client)
    assert client.get("/api/workspace/binding").json()["status"] == "needs_decision"

    job = client.post("/api/workspace/sync").json()
    finished = _wait(client, job["job_id"])
    assert finished["status"] == "error"
    assert "Workspace tab" in finished["error"]

    decided = client.post("/api/workspace/binding", json={"action": "upload"}).json()
    assert decided["ok"] is True and decided["requeued"] >= 1
    job = client.post("/api/workspace/sync").json()
    finished = _wait(client, job["job_id"])
    assert finished["status"] == "done"
    assert finished["result"]["pushed"] >= 1
    # The solo episode's change really reached the workspace log.
    from sqlalchemy import select

    from cloud.models import Change

    with make_sessionmaker(cloud_backend["engine"])() as s:
        assert s.scalar(select(Change).where(Change.table_name == "episodes"))


def test_server_url_roundtrip(client, isolated_env):
    body = client.get("/api/workspace").json()
    assert body["server_url"] == "https://cloud.vetromar.test"
    r = client.post(
        "/api/workspace/server-url", json={"url": "https://ws.example.com/"}
    )
    assert r.status_code == 200
    assert r.json() == {"url": "https://ws.example.com"}
    # Persisted to config.toml (env still overrides in this fixture, so read
    # the file directly).
    assert 'cloud_api_url = "https://ws.example.com"' in (
        (isolated_env / "config.toml").read_text()
    )
    assert (
        client.post("/api/workspace/server-url", json={"url": "not-a-url"}).status_code
        == 400
    )


def test_open_signup_targets_the_server(client, monkeypatch):
    import webbrowser

    seen = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: seen.setdefault("url", url) or True)
    r = client.post("/api/workspace/open-signup").json()
    assert r["url"] == "https://cloud.vetromar.test/signup"
    assert r["opened"] is True and seen["url"] == r["url"]
