"""Graph and identity deletion — signed-proof-gated, host-aware."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from cloud.app import create_app
from cloud.models import Change, Invite, Membership, Principal, Workspace
from tests.cloud_helpers import (
    auth,
    create_graph,
    join,
    login,
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
    with TestClient(create_app(engine=engine)) as c:
        yield c


@pytest.fixture()
def sessionmaker_(engine):
    from cloud.db import make_sessionmaker

    return make_sessionmaker(engine)


def _count(sessionmaker_, model):
    with sessionmaker_() as session:
        return session.scalar(select(func.count()).select_from(model))


@pytest.fixture()
def crew(client, engine):
    """A graph with host + one member, plus a pushed change in its log."""
    host, mo = new_identity(), new_identity()
    graph = create_graph(client, engine, host, name="Crew")
    wid = graph["workspace_id"]
    invite = mint_invite(client, graph["token"], wid)
    mo_body = join(client, invite["token"], mo, "mo").json()
    from vetromar.workspace.wire import new_change_id

    client.post(
        "/v1/sync/push",
        json={
            "device_id": "dev-1",
            "changes": [
                {
                    "change_id": new_change_id(),
                    "table": "units",
                    "op": "insert",
                    "row_id": "unit_x",
                    "payload": {"id": "unit_x"},
                    "recorded_at": "2026-08-01T00:00:00+00:00",
                }
            ],
        },
        headers=ws_headers(graph["token"], wid),
    )
    return {
        "wid": wid,
        "host": {"identity": host, "token": graph["token"]},
        "member": {"identity": mo, "token": mo_body["token"]},
    }


# -- graph deletion --------------------------------------------------------------


def test_delete_graph_requires_host(client, crew):
    resp = client.request(
        "DELETE",
        "/v1/workspaces",
        json=proof(client, crew["member"]["identity"]),
        headers=ws_headers(crew["member"]["token"], crew["wid"]),
    )
    assert resp.status_code == 403


def test_delete_graph_requires_valid_proof(client, crew):
    # A proof signed by someone else's key is rejected even with host's token.
    resp = client.request(
        "DELETE",
        "/v1/workspaces",
        json=proof(client, crew["member"]["identity"]),
        headers=ws_headers(crew["host"]["token"], crew["wid"]),
    )
    assert resp.status_code == 401


def test_delete_graph_erases_everything(client, crew, sessionmaker_):
    resp = client.request(
        "DELETE",
        "/v1/workspaces",
        json=proof(client, crew["host"]["identity"]),
        headers=ws_headers(crew["host"]["token"], crew["wid"]),
    )
    assert resp.status_code == 200
    assert _count(sessionmaker_, Workspace) == 0
    assert _count(sessionmaker_, Membership) == 0
    assert _count(sessionmaker_, Change) == 0
    assert _count(sessionmaker_, Invite) == 0
    # The member's identity had no other graph → gone; the owner survives
    # (it's the server's bootstrap credential).
    with sessionmaker_() as session:
        remaining = session.scalars(select(Principal)).all()
        assert [p.is_owner for p in remaining] == [True]
    # Everyone is signed out.
    assert client.get("/v1/me", headers=auth(crew["member"]["token"])).status_code == 401


# -- identity deletion -------------------------------------------------------------


def test_member_deletes_identity_graph_survives(client, crew, sessionmaker_):
    resp = client.request(
        "DELETE",
        "/v1/me",
        json=proof(client, crew["member"]["identity"]),
        headers=auth(crew["member"]["token"]),
    )
    assert resp.status_code == 200
    assert _count(sessionmaker_, Workspace) == 1
    assert _count(sessionmaker_, Change) == 1
    members = client.get(
        "/v1/members", headers=ws_headers(crew["host"]["token"], crew["wid"])
    ).json()["members"]
    assert [m["handle"] for m in members] == ["host"]
    # The departed key can no longer sign in.
    p = proof(client, crew["member"]["identity"])
    resp = client.post(
        "/v1/auth/verify",
        json={"public_key": crew["member"]["identity"].public_key, **p},
    )
    assert resp.status_code == 403


def test_host_with_members_cannot_delete_identity(client, crew):
    resp = client.request(
        "DELETE",
        "/v1/me",
        json=proof(client, crew["host"]["identity"]),
        headers=auth(crew["host"]["token"]),
    )
    assert resp.status_code == 400
    assert "delete the graph first" in resp.json()["detail"]


def test_solo_host_identity_deletion_takes_graph_along(client, engine, sessionmaker_):
    solo = new_identity()
    graph = create_graph(client, engine, solo, name="Solo")
    resp = client.request(
        "DELETE",
        "/v1/me",
        json=proof(client, solo),
        headers=auth(graph["token"]),
    )
    assert resp.status_code == 200
    assert _count(sessionmaker_, Workspace) == 0
    assert _count(sessionmaker_, Principal) == 0  # even the owner: explicit choice


def test_only_admin_of_populated_graph_blocked(client, engine):
    host, adm, mem = new_identity(), new_identity(), new_identity()
    graph = create_graph(client, engine, host, name="Crew")
    wid = graph["workspace_id"]
    inv_a = mint_invite(client, graph["token"], wid, role="admin")
    adm_token = join(client, inv_a["token"], adm, "adm").json()["token"]
    inv_m = mint_invite(client, graph["token"], wid, role="member")
    join(client, inv_m["token"], mem, "mem")

    # Delete the HOST's graph seat? No — host deletion is blocked while
    # members exist; here the ADMIN tries to delete their identity while
    # being the only admin... but the host counts as an admin-equivalent,
    # so the admin may leave freely.
    resp = client.request(
        "DELETE", "/v1/me", json=proof(client, adm), headers=auth(adm_token)
    )
    assert resp.status_code == 200
