"""Cloud service: keypair auth, graphs, invites, roles, membership lifecycle."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from cloud.app import create_app
from cloud.models import AuthChallenge, Invite, Token, utcnow
from tests.cloud_helpers import (
    auth,
    create_graph,
    join,
    login,
    make_owner,
    mint_invite,
    new_identity,
    proof,
    ws_headers,
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
    app = create_app(engine=engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sessionmaker_(engine):
    from cloud.db import make_sessionmaker

    return make_sessionmaker(engine)


# -- challenge / verify --------------------------------------------------------


def test_challenge_verify_flow(client, engine):
    owner = new_identity()
    make_owner(engine, owner)
    body = login(client, owner)
    assert body["token"]
    assert body["principal"]["public_key"] == owner.public_key
    assert body["principal"]["is_owner"] is True

    me = client.get("/v1/me", headers=auth(body["token"]))
    assert me.status_code == 200
    assert me.json()["workspaces"] == []


def test_unknown_key_cannot_sign_in(client):
    stranger = new_identity()
    resp = client.post(
        "/v1/auth/verify",
        json={"public_key": stranger.public_key, **proof(client, stranger)},
    )
    assert resp.status_code == 403
    assert "not enrolled" in resp.json()["detail"]


def test_bad_signature_rejected(client, engine):
    owner, imposter = new_identity(), new_identity()
    make_owner(engine, owner)
    nonce = client.post(
        "/v1/auth/challenge", json={"public_key": owner.public_key}
    ).json()["nonce"]
    resp = client.post(
        "/v1/auth/verify",
        json={
            "public_key": owner.public_key,
            "nonce": nonce,
            "signature": imposter.sign(nonce),  # wrong key signed it
        },
    )
    assert resp.status_code == 401


def test_challenge_single_use_and_expiry(client, engine, sessionmaker_):
    owner = new_identity()
    make_owner(engine, owner)
    p = proof(client, owner)
    body = {"public_key": owner.public_key, **p}
    assert client.post("/v1/auth/verify", json=body).status_code == 200
    # Replay: the same signed nonce must not work twice.
    assert client.post("/v1/auth/verify", json=body).status_code == 401

    p2 = proof(client, owner)
    with sessionmaker_() as session:
        for row in session.scalars(select(AuthChallenge)).all():
            row.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
    resp = client.post(
        "/v1/auth/verify", json={"public_key": owner.public_key, **p2}
    )
    assert resp.status_code == 401


def test_me_requires_token(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=auth("bogus")).status_code == 401


# -- workspaces ------------------------------------------------------------------


def test_only_owner_creates_graphs(client, engine):
    owner, member = new_identity(), new_identity()
    graph = create_graph(client, engine, owner, name="Crew")
    assert graph["role"] == "host"
    assert graph["handle"] == "host"

    invite = mint_invite(client, graph["token"], graph["workspace_id"])
    joined = join(client, invite["token"], member, "mo").json()
    resp = client.post(
        "/v1/workspaces",
        json={"name": "Rogue", "handle": "mo", "display_name": "Mo"},
        headers=auth(joined["token"]),
    )
    assert resp.status_code == 403
    assert "server owner" in resp.json()["detail"]


def test_one_identity_many_graphs(client, engine):
    owner, mo = new_identity(), new_identity()
    g1 = create_graph(client, engine, owner, name="Crew")
    token = g1["token"]
    resp = client.post(
        "/v1/workspaces",
        json={"name": "Book club", "handle": "host", "display_name": "The Host"},
        headers=auth(token),
    )
    assert resp.status_code == 201
    g2 = resp.json()

    for ws in (g1, g2):
        invite = mint_invite(client, token, ws["workspace_id"])
        resp = join(client, invite["token"], mo, "mo")
        assert resp.status_code == 201, resp.text

    listed = client.get(
        "/v1/workspaces", headers=auth(login(client, mo)["token"])
    ).json()["workspaces"]
    assert {w["workspace_name"] for w in listed} == {"Crew", "Book club"}
    assert all(w["role"] == "member" for w in listed)


def test_workspace_header_required_and_membership_checked(client, engine):
    owner, stranger = new_identity(), new_identity()
    graph = create_graph(client, engine, owner)
    # No header → 400.
    assert (
        client.get("/v1/members", headers=auth(graph["token"])).status_code == 400
    )
    # A second graph's host is not a member of the first... simulate with a
    # non-member principal enrolled via another graph.
    other = create_graph(
        client, engine, stranger, name="Other", handle="s", display_name="S"
    )
    resp = client.get(
        "/v1/members", headers=ws_headers(other["token"], graph["workspace_id"])
    )
    assert resp.status_code == 403


# -- invites + handles ------------------------------------------------------------


def test_invite_flow_with_handles(client, engine):
    owner, mo = new_identity(), new_identity()
    graph = create_graph(client, engine, owner, name="Crew")
    invite = mint_invite(client, graph["token"], graph["workspace_id"])
    assert invite["url_path"] == f"/invite-accept?token={invite['token']}"

    joined = join(client, invite["token"], mo, "Mo", "Mo Member")
    assert joined.status_code == 201
    body = joined.json()
    assert body["role"] == "member"
    assert body["handle"] == "mo"  # normalized lowercase
    assert body["token"]  # joining IS signing in

    members = client.get(
        "/v1/members", headers=ws_headers(graph["token"], graph["workspace_id"])
    ).json()["members"]
    assert {m["handle"] for m in members} == {"host", "mo"}

    # Single use.
    resp = join(client, invite["token"], new_identity(), "x")
    assert resp.status_code == 400
    assert "already been used" in resp.json()["detail"]


def test_duplicate_handle_rejected(client, engine):
    owner = new_identity()
    graph = create_graph(client, engine, owner, handle="ada")
    invite = mint_invite(client, graph["token"], graph["workspace_id"])
    resp = join(client, invite["token"], new_identity(), "ADA")
    assert resp.status_code == 400
    assert "taken" in resp.json()["detail"]


def test_expired_invite_rejected(client, engine, sessionmaker_):
    owner = new_identity()
    graph = create_graph(client, engine, owner)
    invite = mint_invite(client, graph["token"], graph["workspace_id"])
    with sessionmaker_() as session:
        row = session.scalars(select(Invite)).one()
        row.expires_at = utcnow() - timedelta(days=1)
        session.commit()
    resp = join(client, invite["token"], new_identity(), "late")
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]


def test_invite_accept_requires_key_possession(client, engine):
    """Enrolling someone ELSE's public key must fail: the signature is by the
    wrong key."""
    owner, victim, attacker = new_identity(), new_identity(), new_identity()
    graph = create_graph(client, engine, owner)
    invite = mint_invite(client, graph["token"], graph["workspace_id"])
    p = proof(client, attacker)  # attacker signs with their own key...
    resp = client.post(
        "/v1/invites/accept",
        json={
            "token": invite["token"],
            "public_key": victim.public_key,  # ...but claims the victim's
            "handle": "v",
            "display_name": "V",
            **p,
        },
    )
    assert resp.status_code == 401


# -- roles --------------------------------------------------------------------


def _crew(client, engine):
    """host + admin + member, ready to go."""
    owner, adm, mem = new_identity(), new_identity(), new_identity()
    graph = create_graph(client, engine, owner, name="Crew")
    wid = graph["workspace_id"]
    inv_admin = mint_invite(client, graph["token"], wid, role="admin")
    admin_body = join(client, inv_admin["token"], adm, "adm").json()
    inv_member = mint_invite(client, graph["token"], wid, role="member")
    member_body = join(client, inv_member["token"], mem, "mem").json()
    return {
        "wid": wid,
        "host": {"identity": owner, "token": graph["token"]},
        "admin": {"identity": adm, "token": admin_body["token"]},
        "member": {"identity": mem, "token": member_body["token"]},
    }


def _principal_id(client, crew, token_of, handle):
    members = client.get(
        "/v1/members", headers=ws_headers(token_of, crew["wid"])
    ).json()["members"]
    return next(m["principal_id"] for m in members if m["handle"] == handle)


def test_role_matrix(client, engine):
    crew = _crew(client, engine)
    wid = crew["wid"]

    # Admin can invite members but not mint admin invites.
    assert (
        client.post(
            "/v1/invites",
            json={"role": "member"},
            headers=ws_headers(crew["admin"]["token"], wid),
        ).status_code
        == 201
    )
    resp = client.post(
        "/v1/invites",
        json={"role": "admin"},
        headers=ws_headers(crew["admin"]["token"], wid),
    )
    assert resp.status_code == 403

    # Member can invite nobody.
    assert (
        client.post(
            "/v1/invites",
            json={"role": "member"},
            headers=ws_headers(crew["member"]["token"], wid),
        ).status_code
        == 403
    )


def test_role_change_host_only(client, engine):
    crew = _crew(client, engine)
    wid = crew["wid"]
    mem_id = _principal_id(client, crew, crew["host"]["token"], "mem")
    host_id = _principal_id(client, crew, crew["host"]["token"], "host")

    # Admin cannot change roles.
    resp = client.post(
        f"/v1/members/{mem_id}/role",
        json={"role": "admin"},
        headers=ws_headers(crew["admin"]["token"], wid),
    )
    assert resp.status_code == 403

    # Host promotes member → admin.
    resp = client.post(
        f"/v1/members/{mem_id}/role",
        json={"role": "admin"},
        headers=ws_headers(crew["host"]["token"], wid),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"

    # The host role itself is immutable.
    resp = client.post(
        f"/v1/members/{host_id}/role",
        json={"role": "member"},
        headers=ws_headers(crew["host"]["token"], wid),
    )
    assert resp.status_code == 400


def test_removal_rules_and_token_revocation(client, engine):
    crew = _crew(client, engine)
    wid = crew["wid"]
    mem_id = _principal_id(client, crew, crew["host"]["token"], "mem")
    adm_id = _principal_id(client, crew, crew["host"]["token"], "adm")
    host_id = _principal_id(client, crew, crew["host"]["token"], "host")

    # The host cannot be removed.
    resp = client.delete(
        f"/v1/members/{host_id}", headers=ws_headers(crew["host"]["token"], wid)
    )
    assert resp.status_code == 400

    # An admin cannot remove another admin — only the host can.
    resp = client.delete(
        f"/v1/members/{adm_id}", headers=ws_headers(crew["admin"]["token"], wid)
    )
    assert resp.status_code == 403

    # Admin removes a member; tokens die instantly.
    resp = client.delete(
        f"/v1/members/{mem_id}", headers=ws_headers(crew["admin"]["token"], wid)
    )
    assert resp.status_code == 204
    assert (
        client.get("/v1/me", headers=auth(crew["member"]["token"])).status_code == 401
    )
    # Sign-in works again (the identity survives) but membership is gone.
    fresh = login(client, crew["member"]["identity"])
    assert fresh["workspaces"] == []

    # A new invite re-activates the old seat.
    invite = mint_invite(client, crew["host"]["token"], wid)
    rejoined = join(client, invite["token"], crew["member"]["identity"], "mem2")
    assert rejoined.status_code == 201
    assert rejoined.json()["handle"] == "mem2"


# -- tokens + devices -----------------------------------------------------------


def test_token_expiry_and_sliding_bump(client, engine, sessionmaker_):
    owner = new_identity()
    make_owner(engine, owner)
    token = login(client, owner)["token"]
    with sessionmaker_() as session:
        row = session.scalars(select(Token)).one()
        row.expires_at = utcnow() + timedelta(days=1)
        session.commit()
    assert client.get("/v1/me", headers=auth(token)).status_code == 200
    with sessionmaker_() as session:
        row = session.scalars(select(Token)).one()
        assert row.expires_at > utcnow() + timedelta(days=29)
        row.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
    assert client.get("/v1/me", headers=auth(token)).status_code == 401


def test_device_registration_per_workspace(client, engine):
    owner = new_identity()
    g1 = create_graph(client, engine, owner, name="Crew")
    resp = client.post(
        "/v1/workspaces",
        json={"name": "Book club", "handle": "host", "display_name": "The Host"},
        headers=auth(g1["token"]),
    )
    g2 = resp.json()
    # The SAME device id registers cleanly in both graphs.
    for ws in (g1, g2):
        resp = client.put(
            "/v1/devices/dev-abc",
            json={"name": "Ada's MacBook"},
            headers=ws_headers(g1["token"], ws["workspace_id"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"device_id": "dev-abc", "name": "Ada's MacBook"}


def test_invite_page_served_same_origin(client):
    r = client.get("/invite-accept")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "invited" in r.text
    # Retired pages are gone.
    assert client.get("/signup").status_code == 404
    assert client.get("/reset-password").status_code == 404
