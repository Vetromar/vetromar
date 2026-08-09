"""Cloud service: accounts, invites, membership lifecycle."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from cloud.app import create_app
from cloud.models import Invite, Token, Workspace, utcnow


@pytest.fixture()
def engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture()
def client(engine):
    app = create_app(engine=engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sessionmaker_(engine):
    from cloud.db import make_sessionmaker

    return make_sessionmaker(engine)


SIGNUP = {
    "workspace_name": "Acme Corp",
    "name": "Ada Admin",
    "email": "ada@acme.test",
    "password": "hunter22hunter22",
}


def signup(client, **overrides):
    body = {**SIGNUP, **overrides}
    resp = client.post("/v1/workspaces", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_signup_login_me(client):
    created = signup(client)
    assert created["token"]
    assert created["role"] == "admin"
    assert created["workspace"]["name"] == "Acme Corp"

    resp = client.post(
        "/v1/auth/login", json={"email": "ADA@acme.test ", "password": SIGNUP["password"]}
    )
    assert resp.status_code == 200
    token = resp.json()["token"]
    assert token != created["token"]  # fresh token per login

    me = client.get("/v1/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "ada@acme.test"


def test_wrong_password_and_duplicate_email(client):
    signup(client)
    resp = client.post(
        "/v1/auth/login", json={"email": SIGNUP["email"], "password": "wrong-password"}
    )
    assert resp.status_code == 401
    resp = client.post("/v1/workspaces", json=SIGNUP)
    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


def test_short_password_rejected(client):
    resp = client.post("/v1/workspaces", json={**SIGNUP, "password": "short"})
    assert resp.status_code == 400


def test_me_requires_token(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=auth("bogus")).status_code == 401


def test_invite_flow(client):
    admin = signup(client)
    resp = client.post("/v1/invites", json={}, headers=auth(admin["token"]))
    assert resp.status_code == 201
    invite = resp.json()
    assert invite["url_path"] == f"/invite-accept?token={invite['token']}"

    resp = client.post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "Mo Member",
            "email": "mo@acme.test",
            "password": "memberpass123",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["workspace"] == "Acme Corp"

    # Member can log in and sees the same workspace, role=member.
    login = client.post(
        "/v1/auth/login", json={"email": "mo@acme.test", "password": "memberpass123"}
    )
    assert login.status_code == 200
    assert login.json()["role"] == "member"
    assert login.json()["workspace"]["id"] == admin["workspace"]["id"]

    members = client.get("/v1/members", headers=auth(admin["token"])).json()["members"]
    assert {m["email"] for m in members} == {"ada@acme.test", "mo@acme.test"}

    # Single-use: the same invite token is now rejected.
    resp = client.post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "X",
            "email": "x@acme.test",
            "password": "xpassword123",
        },
    )
    assert resp.status_code == 400
    assert "already been used" in resp.json()["detail"]


def test_member_cannot_invite(client):
    admin = signup(client)
    invite = client.post("/v1/invites", json={}, headers=auth(admin["token"])).json()
    client.post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "Mo",
            "email": "mo@acme.test",
            "password": "memberpass123",
        },
    )
    member_token = client.post(
        "/v1/auth/login", json={"email": "mo@acme.test", "password": "memberpass123"}
    ).json()["token"]
    resp = client.post("/v1/invites", json={}, headers=auth(member_token))
    assert resp.status_code == 403


def test_expired_invite_rejected(client, sessionmaker_):
    admin = signup(client)
    invite = client.post("/v1/invites", json={}, headers=auth(admin["token"])).json()
    with sessionmaker_() as session:
        row = session.scalars(select(Invite)).one()
        row.expires_at = utcnow() - timedelta(days=1)
        session.commit()
    resp = client.post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "Late",
            "email": "late@acme.test",
            "password": "latepassword1",
        },
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]


def test_member_removal_revokes_tokens(client):
    admin = signup(client)
    invite = client.post("/v1/invites", json={}, headers=auth(admin["token"])).json()
    client.post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "Mo",
            "email": "mo@acme.test",
            "password": "memberpass123",
        },
    )
    login = client.post(
        "/v1/auth/login", json={"email": "mo@acme.test", "password": "memberpass123"}
    ).json()
    member_token = login["token"]
    member_id = login["user"]["id"]

    resp = client.delete(f"/v1/members/{member_id}", headers=auth(admin["token"]))
    assert resp.status_code == 204
    # Token revoked immediately.
    assert client.get("/v1/me", headers=auth(member_token)).status_code == 401
    # Login still authenticates the password but there is no active membership.
    resp = client.post(
        "/v1/auth/login", json={"email": "mo@acme.test", "password": "memberpass123"}
    )
    assert resp.status_code == 403


def test_last_admin_cannot_be_removed(client):
    admin = signup(client)
    resp = client.delete(
        f"/v1/members/{admin['user']['id']}", headers=auth(admin["token"])
    )
    assert resp.status_code == 400
    assert "only admin" in resp.json()["detail"]


def test_token_expiry(client, sessionmaker_):
    admin = signup(client)
    with sessionmaker_() as session:
        for token in session.scalars(select(Token)).all():
            token.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
    assert client.get("/v1/me", headers=auth(admin["token"])).status_code == 401


def test_sliding_expiry_bumps(client, sessionmaker_):
    admin = signup(client)
    with sessionmaker_() as session:
        token = session.scalars(select(Token)).one()
        token.expires_at = utcnow() + timedelta(days=1)
        session.commit()
    assert client.get("/v1/me", headers=auth(admin["token"])).status_code == 200
    with sessionmaker_() as session:
        token = session.scalars(select(Token)).one()
        assert token.expires_at > utcnow() + timedelta(days=29)


def test_device_registration(client):
    admin = signup(client)
    resp = client.put(
        "/v1/devices/dev-abc", json={"name": "Ada's MacBook"}, headers=auth(admin["token"])
    )
    assert resp.status_code == 200
    assert resp.json() == {"device_id": "dev-abc", "name": "Ada's MacBook"}
    # Idempotent re-register keeps the name when none given.
    resp = client.put("/v1/devices/dev-abc", json={}, headers=auth(admin["token"]))
    assert resp.json()["name"] == "Ada's MacBook"



def test_account_pages_served_same_origin(client):
    # N self-hosted servers can't share a static site with a baked API URL —
    # the server serves its own signup/invite/reset pages.
    for path, marker in (
        ("/signup", "Create your workspace"),
        ("/invite-accept", "Join your team"),
        ("/reset-password", "Choose a new password"),
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
        assert marker in r.text
        # Same-origin fetches only — no external API constant in the page.
        assert "vetromar.com" not in r.text
