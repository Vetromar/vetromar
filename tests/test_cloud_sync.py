"""Cloud service: the replication log (push/pull)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cloud.app import create_app
from cloud.db import make_sessionmaker
from cloud.models import Workspace, utcnow
from vetromar.workspace.wire import new_change_id


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


def make_workspace(client, engine=None, email="ada@acme.test", name="Acme"):
    resp = client.post(
        "/v1/workspaces",
        json={
            "workspace_name": name,
            "name": "Admin",
            "email": email,
            "password": "hunter22hunter22",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    return {"Authorization": f"Bearer {body['token']}"}, body


def change(row_id="unit_aaa", table="units", op="insert", payload=None):
    return {
        "change_id": new_change_id(),
        "table": table,
        "op": op,
        "row_id": row_id,
        "payload": payload or {"id": row_id, "content": "x"},
        "recorded_at": "2026-07-20T00:00:00+00:00",
    }


def push(client, headers, changes, device="dev-1"):
    return client.post(
        "/v1/sync/push", json={"device_id": device, "changes": changes}, headers=headers
    )


def test_push_assigns_contiguous_seq(client, engine):
    headers, _ = make_workspace(client, engine)
    resp = push(client, headers, [change(), change(), change()])
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 3, "duplicates": 0, "last_seq": 3}
    resp = push(client, headers, [change()])
    assert resp.json()["last_seq"] == 4


def test_duplicate_change_ids_skipped(client, engine):
    headers, _ = make_workspace(client, engine)
    c1 = change()
    assert push(client, headers, [c1]).json()["accepted"] == 1
    # Re-push after a lost ack: same change_id, plus a batch-internal dup.
    c2 = change()
    resp = push(client, headers, [c1, c2, c2])
    assert resp.json() == {"accepted": 1, "duplicates": 2, "last_seq": 2}


def test_pull_from_zero_ordered_and_paged(client, engine):
    headers, _ = make_workspace(client, engine)
    changes = [change(row_id=f"unit_{i:03d}") for i in range(5)]
    push(client, headers, changes, device="dev-1")

    resp = client.get("/v1/sync/pull", params={"since": 0, "limit": 2}, headers=headers)
    body = resp.json()
    assert [c["seq"] for c in body["changes"]] == [1, 2]
    assert body["has_more"] is True
    assert body["next_since"] == 2
    assert body["changes"][0]["origin_device_id"] == "dev-1"

    resp = client.get(
        "/v1/sync/pull", params={"since": body["next_since"]}, headers=headers
    )
    body = resp.json()
    assert [c["seq"] for c in body["changes"]] == [3, 4, 5]
    assert body["has_more"] is False
    assert body["next_since"] == 5
    # Payload round-trips.
    assert body["changes"][0]["payload"]["id"] == "unit_002"


def test_workspace_isolation(client, engine):
    headers_a, _ = make_workspace(client, engine, email="a@acme.test", name="A")
    headers_b, _ = make_workspace(client, engine, email="b@bcorp.test", name="B")
    push(client, headers_a, [change()])
    resp = client.get("/v1/sync/pull", headers=headers_b)
    assert resp.json()["changes"] == []
    # B's log has its own seq space.
    resp = push(client, headers_b, [change()])
    assert resp.json()["last_seq"] == 1


def test_push_requires_auth(client):
    resp = client.post("/v1/sync/push", json={"device_id": "d", "changes": []})
    assert resp.status_code == 401
