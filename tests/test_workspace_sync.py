"""The multi-device simulation: two client Stores syncing through the real

in-process cloud app (httpx.ASGITransport — the same code path as a real
deployment, minus the socket).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cloud.app import create_app
from tests.cloud_helpers import make_owner, new_identity
from vetromar.ingest.manual import (
    add_draft,
    add_source_episode,
    create_entity,
    link_alias,
    supersede,
)
from vetromar.schema import ClaimPayload, ExcerptEvidence, PersonRef, UnitDraft
from vetromar.store import Store
from vetromar.workspace.client import CloudClient, WorkspaceError
from vetromar.workspace.engine import CURSOR_KEY, sync_workspace

WHEN = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
RAW = "Priya said the invoicing carve-out ships next sprint. Everyone agreed."


@pytest.fixture()
def cloud():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    app = create_app(engine=engine)

    def make_client(token=None, workspace_id=None):
        # TestClient IS a sync httpx.Client over the ASGI app — the exact
        # transport-injection seam CloudClient exposes for tests.
        return CloudClient(
            base_url="http://testserver",
            token=token,
            workspace_id=workspace_id,
            http=TestClient(app),
        )

    yield {"make_client": make_client, "engine": engine}


@pytest.fixture()
def team(cloud):
    """A graph with two signed-in member clients (host + member), keypair-era."""
    host_identity, member_identity = new_identity(), new_identity()
    make_owner(cloud["engine"], host_identity)

    bootstrap = cloud["make_client"]()
    bootstrap.login_with_key(host_identity)
    ws = bootstrap.create_workspace("Acme", "ada", "Ada")
    workspace_id = ws["workspace_id"]

    host = cloud["make_client"](workspace_id=workspace_id)
    host.login_with_key(host_identity)
    invite = host.create_invite()
    member = cloud["make_client"](workspace_id=workspace_id)
    member.accept_invite(invite["token"], member_identity, "mo", "Mo")
    return {
        "admin": host,
        "member": member,
        "workspace_id": workspace_id,
        "engine": cloud["engine"],
        "host_identity": host_identity,
        "member_identity": member_identity,
    }


def draft(text="the invoicing carve-out ships next sprint"):
    return UnitDraft(
        content=f"Claim: {text}",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=text, author=PersonRef(ref="Priya"))],
    )


def seed_capture(store):
    ep = add_source_episode(
        store, title="Thread", source_kind="chat", raw=RAW, occurred_at=WHEN
    )
    unit = add_draft(store, ep.id, draft(), valid_from=WHEN)
    entity = create_entity(store, "Priya")
    store.add_edge(unit.id, entity.id, kind="mentions")
    return ep, unit, entity


def test_capture_on_a_appears_on_b(team):
    a, b = Store(":memory:"), Store(":memory:")
    ep, unit, entity = seed_capture(a)

    report_a = sync_workspace(a, team["admin"], "dev-a")
    assert report_a.pushed == 4
    assert report_a.pulled == 4  # own changes come back...
    assert report_a.skipped == 4  # ...and are self-skipped

    report_b = sync_workspace(b, team["member"], "dev-b")
    assert report_b.applied == 4
    assert b.get_episode(ep.id).raw == RAW
    assert b.get_unit(unit.id).model_dump() == unit.model_dump()
    assert b.get_entity(entity.id).name == "Priya"
    assert len(b.edges_for(unit.id)) == 1
    assert b.search_fts("invoicing carve-out")[0][0].id == unit.id

    # Re-sync both: clean no-ops, cursor stable.
    assert sync_workspace(a, team["admin"], "dev-a").pulled == 0
    assert sync_workspace(b, team["member"], "dev-b").pulled == 0
    a.close(), b.close()


def test_supersede_propagates(team):
    a, b = Store(":memory:"), Store(":memory:")
    ep, old, _ = seed_capture(a)
    sync_workspace(a, team["admin"], "dev-a")
    sync_workspace(b, team["member"], "dev-b")

    new = add_draft(
        a, ep.id, draft("Everyone agreed"), valid_from=WHEN + timedelta(days=1)
    )
    supersede(a, old.id, new.id)
    sync_workspace(a, team["admin"], "dev-a")
    report = sync_workspace(b, team["member"], "dev-b")
    assert report.quarantined == 0
    assert b.get_unit(old.id).valid_to == WHEN + timedelta(days=1)
    assert [e.kind for e in b.edges_for(new.id)] == ["supersedes"]
    a.close(), b.close()


def test_concurrent_edits_both_sides_converge(team):
    """A and B both write before either syncs; both converge afterwards."""
    a, b = Store(":memory:"), Store(":memory:")
    ep_a, unit_a, _ = seed_capture(a)
    ep_b = add_source_episode(
        b, title="Note", source_kind="note", raw="ship it friday", occurred_at=WHEN
    )
    unit_b = add_draft(b, ep_b.id, draft("ship it friday"), valid_from=WHEN)

    sync_workspace(a, team["admin"], "dev-a")
    sync_workspace(b, team["member"], "dev-b")
    sync_workspace(a, team["admin"], "dev-a")  # picks up B's rows

    for store in (a, b):
        assert store.get_unit(unit_a.id).id == unit_a.id
        assert store.get_unit(unit_b.id).id == unit_b.id
        assert store.quarantine_count() == 0
    a.close(), b.close()


def test_fresh_device_bootstraps_from_zero(team):
    a = Store(":memory:")
    ep, unit, _ = seed_capture(a)
    sync_workspace(a, team["admin"], "dev-a")

    fresh = Store(":memory:")
    report = sync_workspace(fresh, team["member"], "dev-new")
    assert int(fresh.get_replication_state(CURSOR_KEY)) == report.cursor > 0
    assert fresh.get_unit(unit.id).id == unit.id
    a.close(), fresh.close()


def test_preexisting_store_uploads_via_bootstrap_seed(team):
    """A store with data that predates the outbox (migrated v3) still uploads
    everything on first sync."""
    a = Store(":memory:")
    ep, unit, entity = seed_capture(a)
    # Simulate pre-v4 rows: wipe the outbox as if the writes were never logged.
    a._conn.execute("DELETE FROM changelog")
    a._conn.commit()
    assert a.pending_changes() == []

    report = sync_workspace(a, team["admin"], "dev-a")
    assert report.seeded == 4
    assert report.pushed == 4

    b = Store(":memory:")
    sync_workspace(b, team["member"], "dev-b")
    assert b.get_unit(unit.id).id == unit.id
    a.close(), b.close()


def test_repush_after_lost_ack_dedupes(team):
    a = Store(":memory:")
    seed_capture(a)
    records = a.pending_changes(limit=10)
    # First push succeeds server-side but the ack is "lost" (not marked).
    team["admin"].push("dev-a", records)
    report = sync_workspace(a, team["admin"], "dev-a")
    assert report.pushed == 4  # re-sent...
    b = Store(":memory:")
    sync_workspace(b, team["member"], "dev-b")
    # ...but the server deduped: exactly one copy of each row.
    assert len(b.list_units()) == 1
    assert len(b.list_episodes()) == 1
    a.close(), b.close()


def test_cross_device_alias_merge(team):
    a, b = Store(":memory:"), Store(":memory:")
    entity = create_entity(a, "Priya")
    sync_workspace(a, team["admin"], "dev-a")
    sync_workspace(b, team["member"], "dev-b")

    link_alias(a, entity.id, "priya.k")
    link_alias(b, entity.id, "pk-slack")
    sync_workspace(a, team["admin"], "dev-a")
    sync_workspace(b, team["member"], "dev-b")
    sync_workspace(a, team["admin"], "dev-a")

    assert set(a.get_entity(entity.id).aliases) == {"priya.k", "pk-slack"}
    assert set(b.get_entity(entity.id).aliases) == {"priya.k", "pk-slack"}
    a.close(), b.close()


# -- workspace binding (M22: deletion/recreate must not silently cross-sync) --


def test_binding_fresh_empty_store_binds_on_first_sync(team):
    from vetromar.workspace.engine import BOUND_KEY

    a = Store(":memory:")
    sync_workspace(a, team["admin"], "dev-a", workspace_id=team["workspace_id"])
    assert a.get_replication_state(BOUND_KEY) == team["workspace_id"]
    # Once bound, capture + sync flow without any decision.
    seed_capture(a)
    report = sync_workspace(
        a, team["admin"], "dev-a", workspace_id=team["workspace_id"]
    )
    assert report.pushed >= 4
    a.close()


def test_binding_solo_store_asks_before_first_upload(team):
    """A solo-era store (local knowledge, never synced anywhere) must NOT
    bulk-upload on first connect — the human decides first."""
    from vetromar.workspace.client import WorkspaceBindingError
    from vetromar.workspace.engine import binding_status, rebind_and_upload

    a = Store(":memory:")
    seed_capture(a)
    assert binding_status(a, team["workspace_id"]) == "needs_decision"
    with pytest.raises(WorkspaceBindingError):
        sync_workspace(a, team["admin"], "dev-a", workspace_id=team["workspace_id"])

    rebind_and_upload(a, team["workspace_id"])
    report = sync_workspace(
        a, team["admin"], "dev-a", workspace_id=team["workspace_id"]
    )
    assert report.pushed >= 4
    a.close()


def test_binding_mismatch_blocks_sync(team):
    from vetromar.workspace.client import WorkspaceBindingError

    a = Store(":memory:")
    sync_workspace(a, team["admin"], "dev-a", workspace_id=team["workspace_id"])
    with pytest.raises(WorkspaceBindingError):
        sync_workspace(a, team["admin"], "dev-a", workspace_id="ws_other")
    a.close()


def test_binding_legacy_pushed_store_needs_decision(team):
    """A store that synced before binding existed (bound key absent, pushed
    rows present) must ask — it may belong to a deleted workspace."""
    from vetromar.workspace.engine import binding_status

    a = Store(":memory:")
    seed_capture(a)
    sync_workspace(a, team["admin"], "dev-a")  # legacy call: no workspace_id
    assert binding_status(a, team["workspace_id"]) == "needs_decision"
    a.close()


def test_delete_workspace_then_rebind_reuploads_full_graph(cloud, team):
    """The rebind scenario end-to-end: workspace deleted via the API, a new
    one created, the old machine rebinds and its whole graph re-uploads."""
    from vetromar.workspace.engine import rebind_and_upload

    a = Store(":memory:")
    # Bind while empty, then capture and upload the graph.
    sync_workspace(a, team["admin"], "dev-a", workspace_id=team["workspace_id"])
    ep, unit, entity = seed_capture(a)
    sync_workspace(a, team["admin"], "dev-a", workspace_id=team["workspace_id"])

    # Host deletes the graph; this test is about replication.
    team["admin"].delete_workspace(team["host_identity"])

    # Same owner spins up a new graph on the same server.
    reborn = cloud["make_client"]()
    reborn.login_with_key(team["host_identity"])
    new_ws = reborn.create_workspace("Acme Reborn", "ada", "Ada")["workspace_id"]
    new_client = cloud["make_client"](workspace_id=new_ws)
    new_client.login_with_key(team["host_identity"])

    # The old store refuses to sync until the human decides…
    from vetromar.workspace.client import WorkspaceBindingError

    with pytest.raises(WorkspaceBindingError):
        sync_workspace(a, new_client, "dev-a", workspace_id=new_ws)

    # …then 'upload' requeues everything and the graph lands in the new
    # workspace, provable from a second fresh device.
    requeued = rebind_and_upload(a, new_ws)
    assert requeued >= 4
    report = sync_workspace(a, new_client, "dev-a", workspace_id=new_ws)
    assert report.pushed == requeued

    b = Store(":memory:")
    sync_workspace(b, new_client, "dev-b", workspace_id=new_ws)
    assert b.get_unit(unit.id).id == unit.id
    assert b.get_episode(ep.id).raw == RAW
    assert b.get_entity(entity.id).name == "Priya"
    a.close(), b.close()
