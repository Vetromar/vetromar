"""Cloud service: workspace + account deletion (the explicit destructive
path). Password-confirmed; local knowledge is never touched."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from cloud.app import create_app
from cloud.db import make_sessionmaker
from cloud.models import (
    Change,
    Device,
    Invite,
    Membership,
    Token,
    User,
    Workspace,
)


@pytest.fixture()
def engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture()
def client(engine):
    with TestClient(create_app(engine=engine)) as c:
        yield c


@pytest.fixture()
def sessionmaker_(engine):
    return make_sessionmaker(engine)


def signup(client, email="ada@acme.test", workspace_name="Acme"):
    resp = client.post(
        "/v1/workspaces",
        json={
            "workspace_name": workspace_name,
            "name": "Ada Admin",
            "email": email,
            "password": "hunter22hunter22",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def add_member(client, admin_token, email="mem@acme.test"):
    invite = client.post("/v1/invites", headers=auth(admin_token), json={}).json()
    r = client.post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "name": "Mem Ber",
            "email": email,
            "password": "hunter22hunter22",
        },
    )
    assert r.status_code == 201, r.text
    return client.post(
        "/v1/auth/login", json={"email": email, "password": "hunter22hunter22"}
    ).json()


def seed_workspace_rows(sessionmaker_, ws_id, user_id):
    """Rows in every workspace-owned table, so deletion proves the sweep."""
    with sessionmaker_() as s:
        s.add(
            Change(
                workspace_id=ws_id,
                seq=1,
                change_id="chg_1",
                table_name="episodes",
                op="insert",
                row_id="ep_1",
                payload="{}",
                origin_device_id="dev_a",
                origin_user_id=user_id,
                recorded_at="2026-07-22T00:00:00Z",
            )
        )
        s.commit()


def table_counts(sessionmaker_):
    with sessionmaker_() as s:
        return {
            t.__tablename__: s.scalar(select(func.count()).select_from(t))
            for t in (Workspace, User, Membership, Token, Invite, Device, Change)
        }


# -- workspace deletion ------------------------------------------------------


def test_delete_workspace_requires_admin(client):
    body = signup(client)
    member = add_member(client, body["token"])
    r = client.request(
        "DELETE",
        "/v1/workspaces",
        headers=auth(member["token"]),
        json={"password": "hunter22hunter22"},
    )
    assert r.status_code == 403


def test_delete_workspace_wrong_password(client, sessionmaker_):
    body = signup(client)
    r = client.request(
        "DELETE",
        "/v1/workspaces",
        headers=auth(body["token"]),
        json={"password": "not-the-password"},
    )
    assert r.status_code == 403
    assert table_counts(sessionmaker_)["workspaces"] == 1


def test_delete_workspace_erases_everything(client, sessionmaker_):
    body = signup(client)
    ws_id = body["workspace"]["id"]
    member = add_member(client, body["token"])
    client.put(
        "/v1/devices/dev_a", headers=auth(body["token"]), json={"name": "mac"}
    )
    client.post("/v1/invites", headers=auth(body["token"]), json={})  # unused invite
    seed_workspace_rows(sessionmaker_, ws_id, body["user"]["id"])

    r = client.request(
        "DELETE",
        "/v1/workspaces",
        headers=auth(body["token"]),
        json={"password": "hunter22hunter22"},
    )
    assert r.status_code == 200 and r.json() == {"deleted": True}
    counts = table_counts(sessionmaker_)
    assert all(v == 0 for v in counts.values()), counts
    # Every session token is gone — both users are signed out everywhere.
    assert (
        client.get("/v1/me", headers=auth(member["token"])).status_code == 401
    )
    # The email is free again: the recreate-flow works.
    signup(client, email="ada@acme.test")


# -- account deletion --------------------------------------------------------


def test_delete_account_member_leaves_workspace_intact(client, sessionmaker_):
    body = signup(client)
    member = add_member(client, body["token"])

    r = client.request(
        "DELETE",
        "/v1/me",
        headers=auth(member["token"]),
        json={"password": "hunter22hunter22"},
    )
    assert r.status_code == 200 and r.json() == {"deleted": True}
    counts = table_counts(sessionmaker_)
    assert counts["workspaces"] == 1 and counts["users"] == 1
    assert counts["memberships"] == 1
    assert client.get("/v1/me", headers=auth(body["token"])).status_code == 200
    # The member's login is gone for good.
    assert (
        client.post(
            "/v1/auth/login",
            json={"email": "mem@acme.test", "password": "hunter22hunter22"},
        ).status_code
        == 401
    )


def test_delete_account_sole_admin_with_members_refused(client):
    body = signup(client)
    add_member(client, body["token"])
    r = client.request(
        "DELETE",
        "/v1/me",
        headers=auth(body["token"]),
        json={"password": "hunter22hunter22"},
    )
    assert r.status_code == 400
    assert "only admin" in r.json()["detail"]


def test_delete_account_solo_user_deletes_workspace_too(client, sessionmaker_):
    body = signup(client)
    r = client.request(
        "DELETE",
        "/v1/me",
        headers=auth(body["token"]),
        json={"password": "hunter22hunter22"},
    )
    assert r.status_code == 200
    counts = table_counts(sessionmaker_)
    assert all(v == 0 for v in counts.values()), counts


def test_delete_account_wrong_password(client):
    body = signup(client)
    r = client.request(
        "DELETE",
        "/v1/me",
        headers=auth(body["token"]),
        json={"password": "nope-nope-nope-nope"},
    )
    assert r.status_code == 403
