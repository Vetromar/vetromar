"""Cloud service: admin-minted password-reset links + the operator CLI.

The server never sends email — an admin mints a one-time link from the app
(or the operator runs `python -m cloud reset-link` on the server box) and
hands it over any channel. /v1/auth/reset-confirm consumes it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cloud.app import create_app
from cloud.db import make_engine, make_sessionmaker
from cloud.models import ResetToken, Token, utcnow


@pytest.fixture()
def engine():
    return make_engine("sqlite://")


@pytest.fixture()
def client(engine):
    with TestClient(create_app(engine=engine)) as c:
        yield c


@pytest.fixture()
def sessionmaker_(engine):
    return make_sessionmaker(engine)


def signup(client, email="ada@acme.test"):
    resp = client.post(
        "/v1/workspaces",
        json={
            "workspace_name": "Acme",
            "name": "Ada Admin",
            "email": email,
            "password": "hunter22hunter22",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_member(client, admin_token, email="mo@acme.test"):
    """Invite + accept + login: returns the member's /v1/me payload + token."""
    inv = client.post("/v1/invites", headers=auth(admin_token), json={})
    assert inv.status_code == 201, inv.text
    acc = client.post(
        "/v1/invites/accept",
        json={
            "token": inv.json()["token"],
            "name": "Mo Member",
            "email": email,
            "password": "memberpass99",
        },
    )
    assert acc.status_code == 201, acc.text
    login = client.post(
        "/v1/auth/login", json={"email": email, "password": "memberpass99"}
    )
    assert login.status_code == 200, login.text
    return login.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _find_member(client, admin_token, email):
    members = client.get("/v1/members", headers=auth(admin_token)).json()["members"]
    return next(m for m in members if m["email"] == email)


def mint_link(client, admin_token, user_id):
    return client.post(
        f"/v1/members/{user_id}/reset-link", headers=auth(admin_token)
    )


# -- minting -------------------------------------------------------------------


def test_admin_mints_reset_link_for_member(client, sessionmaker_):
    created = signup(client)
    add_member(client, created["token"])
    target = _find_member(client, created["token"], "mo@acme.test")

    r = mint_link(client, created["token"], target["user_id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url_path"] == f"/reset-password?token={body['token']}"
    assert body["email"] == "mo@acme.test"
    assert body["name"] == "Mo Member"
    assert body["expires_at"].endswith("Z")
    with sessionmaker_() as s:
        assert s.scalar(select(ResetToken)) is not None


def test_member_cannot_mint_reset_links(client):
    created = signup(client)
    member = add_member(client, created["token"])
    admin = _find_member(client, created["token"], "ada@acme.test")
    r = mint_link(client, member["token"], admin["user_id"])
    assert r.status_code == 403


def test_unknown_or_removed_target_404s(client):
    created = signup(client)
    assert mint_link(client, created["token"], "usr_nope").status_code == 404

    member = add_member(client, created["token"])
    target = _find_member(client, created["token"], "mo@acme.test")
    rm = client.delete(
        f"/v1/members/{target['user_id']}", headers=auth(created["token"])
    )
    assert rm.status_code == 204
    assert mint_link(client, created["token"], target["user_id"]).status_code == 404
    del member  # membership deactivated; token already revoked by removal


# -- reset-confirm lifecycle (link minted by an admin) -------------------------


def _mint_raw(client, admin_token, email="ada@acme.test"):
    target = _find_member(client, admin_token, email)
    r = mint_link(client, admin_token, target["user_id"])
    assert r.status_code == 201, r.text
    return r.json()["token"]


def test_reset_confirm_changes_password_and_kills_sessions(client, sessionmaker_):
    created = signup(client)
    old_token = created["token"]
    raw = _mint_raw(client, old_token)

    r = client.post(
        "/v1/auth/reset-confirm", json={"token": raw, "password": "newpassword99"}
    )
    assert r.status_code == 200, r.text

    # Every prior session is dead.
    assert client.get("/v1/me", headers=auth(old_token)).status_code == 401
    with sessionmaker_() as s:
        assert s.scalar(select(Token)) is None

    # Old password rejected, new one signs in.
    r = client.post(
        "/v1/auth/login",
        json={"email": "ada@acme.test", "password": "hunter22hunter22"},
    )
    assert r.status_code == 401
    r = client.post(
        "/v1/auth/login", json={"email": "ada@acme.test", "password": "newpassword99"}
    )
    assert r.status_code == 200


def test_reset_token_single_use(client):
    created = signup(client)
    raw = _mint_raw(client, created["token"])
    assert (
        client.post(
            "/v1/auth/reset-confirm", json={"token": raw, "password": "newpassword99"}
        ).status_code
        == 200
    )
    r = client.post(
        "/v1/auth/reset-confirm", json={"token": raw, "password": "anotherpass99"}
    )
    assert r.status_code == 400


def test_reset_token_expires(client, sessionmaker_):
    created = signup(client)
    raw = _mint_raw(client, created["token"])
    with sessionmaker_() as s:
        rt = s.scalar(select(ResetToken))
        rt.expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
    r = client.post(
        "/v1/auth/reset-confirm", json={"token": raw, "password": "newpassword99"}
    )
    assert r.status_code == 400


def test_reset_confirm_garbage_token_and_short_password(client):
    signup(client)
    r = client.post(
        "/v1/auth/reset-confirm", json={"token": "nope", "password": "newpassword99"}
    )
    assert r.status_code == 400
    r = client.post(
        "/v1/auth/reset-confirm", json={"token": "nope", "password": "short"}
    )
    assert r.status_code == 400


# -- operator CLI --------------------------------------------------------------


def test_cli_reset_link_round_trip(tmp_path, monkeypatch, capsys):
    from cloud.__main__ import main

    monkeypatch.setenv("CLOUD_DATABASE_URL", f"sqlite:///{tmp_path / 'cloud.db'}")
    engine = make_engine()
    with TestClient(create_app(engine=engine)) as client:
        signup(client)
        assert main(["reset-link", "Ada@acme.test"]) == 0
        link = capsys.readouterr().out.strip()
        assert "/reset-password?token=" in link
        raw = link.split("token=", 1)[1]
        r = client.post(
            "/v1/auth/reset-confirm", json={"token": raw, "password": "newpassword99"}
        )
        assert r.status_code == 200, r.text
        r = client.post(
            "/v1/auth/login",
            json={"email": "ada@acme.test", "password": "newpassword99"},
        )
        assert r.status_code == 200


def test_cli_reset_link_unknown_email(tmp_path, monkeypatch, capsys):
    from cloud.__main__ import main

    monkeypatch.setenv("CLOUD_DATABASE_URL", f"sqlite:///{tmp_path / 'cloud.db'}")
    assert main(["reset-link", "nobody@acme.test"]) == 1
    assert capsys.readouterr().out == ""
