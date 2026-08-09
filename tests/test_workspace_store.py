"""Store v4: the replication outbox (changelog) + the trusted apply door."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from vetromar.ingest.manual import (
    add_draft,
    add_source_episode,
    create_entity,
    link_alias,
    supersede,
)
from vetromar.schema import ClaimPayload, ExcerptEvidence, PersonRef, UnitDraft
from vetromar.store import Store

WHEN = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
RAW = "Priya said the invoicing carve-out ships next sprint. Everyone agreed."


def draft(text="the invoicing carve-out ships next sprint"):
    return UnitDraft(
        content=f"Claim: {text}",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=text, author=PersonRef(ref="Priya"))],
    )


def seed(store, raw=RAW):
    """Episode with raw + one gated unit; returns (episode, unit)."""
    ep = add_source_episode(
        store, title="Thread", source_kind="chat", raw=raw, occurred_at=WHEN
    )
    unit = add_draft(store, ep.id, draft(), valid_from=WHEN)
    return ep, unit


def envelope(record, seq=1, device="dev-a"):
    return {
        **record,
        "seq": seq,
        "origin_device_id": device,
        "origin_user_id": "usr_x",
        "received_at": "2026-07-20T00:00:00+00:00",
    }


def drain(store):
    """All pending outbox records as server-style envelopes, marked pushed."""
    records = store.pending_changes(limit=1000)
    store.mark_pushed([r["change_id"] for r in records])
    return [envelope(r, seq=i + 1) for i, r in enumerate(records)]


# -- outbox logging ----------------------------------------------------------


def test_every_write_logs_one_change(store):
    ep, unit = seed(store)
    entity = create_entity(store, "Priya")
    link_alias(store, entity.id, "priya.k")
    store.add_edge(unit.id, entity.id, kind="mentions")

    changes = store.pending_changes()
    kinds = [(c["table"], c["op"], c["row_id"]) for c in changes]
    assert kinds == [
        ("episodes", "insert", ep.id),
        ("units", "insert", unit.id),
        ("entities", "insert", entity.id),
        ("entities", "update", entity.id),
        ("edges", "insert", changes[4]["row_id"]),
    ]
    # Payloads are the full model JSON.
    assert changes[0]["payload"]["raw"] == RAW
    assert changes[1]["payload"]["id"] == unit.id
    assert changes[3]["payload"]["aliases"] == ["priya.k"]


def test_supersede_logs_update_and_edge(store):
    ep, old = seed(store)
    new = add_draft(
        store, ep.id, draft("Everyone agreed"), valid_from=WHEN + timedelta(days=1)
    )
    drain(store)
    supersede(store, old.id, new.id)
    changes = store.pending_changes()
    assert [(c["table"], c["op"]) for c in changes] == [
        ("units", "update"),
        ("edges", "insert"),
    ]
    assert changes[0]["row_id"] == old.id
    assert changes[0]["payload"]["valid_to"] is not None


def test_duplicate_edge_and_alias_do_not_log(store):
    ep, unit = seed(store)
    entity = create_entity(store, "Priya")
    store.add_edge(unit.id, entity.id, kind="mentions")
    drain(store)
    store.add_edge(unit.id, entity.id, kind="mentions")  # dedupe drop
    link_alias(store, entity.id, "Priya")  # already the name? no — alias list
    link_alias(store, entity.id, "priya.k")
    link_alias(store, entity.id, "priya.k")  # idempotent re-add
    assert [(c["table"], c["op"]) for c in store.pending_changes()] == [
        ("entities", "update"),
        ("entities", "update"),
    ]


def test_rename_logs_one_episode_update(store):
    ep, _ = seed(store)
    drain(store)
    store.rename_episode(ep.id, "Renamed thread")
    changes = store.pending_changes()
    assert [(c["table"], c["op"], c["row_id"]) for c in changes] == [
        ("episodes", "update", ep.id)
    ]
    assert changes[0]["payload"]["title"] == "Renamed thread"


def test_sync_state_and_embeddings_do_not_log(store):
    store.set_sync_state("notion", '{"cursor": 1}')
    _, unit = seed(store)
    drain(store)
    store.put_embedding(unit.id, b"\x00\x00\x80?")
    assert store.pending_changes() == []


def test_mark_pushed(store):
    seed(store)
    records = store.pending_changes()
    assert len(records) == 2
    store.mark_pushed([records[0]["change_id"]])
    remaining = store.pending_changes()
    assert [r["change_id"] for r in remaining] == [records[1]["change_id"]]


# -- apply -------------------------------------------------------------------


def replicate(source_store, target_store):
    outcomes = [target_store.apply_change(env) for env in drain(source_store)]
    return outcomes


def test_apply_replicates_and_is_idempotent(store):
    ep, unit = seed(store)
    entity = create_entity(store, "Priya")
    store.add_edge(unit.id, entity.id, kind="mentions")
    envs = drain(store)

    replica = Store(":memory:")
    outcomes = [replica.apply_change(e) for e in envs]
    assert outcomes == ["applied"] * 4

    assert replica.get_episode(ep.id).raw == RAW
    got = replica.get_unit(unit.id)
    assert got.model_dump() == unit.model_dump()
    assert replica.get_entity(entity.id).name == "Priya"
    assert len(replica.edges_for(unit.id)) == 1
    # FTS was rebuilt on apply.
    assert replica.search_fts("invoicing carve-out")[0][0].id == unit.id
    # Applying a remote change never writes the replica's own outbox.
    assert replica.pending_changes() == []

    # Second apply: all no-ops.
    assert [replica.apply_change(e) for e in envs] == ["skipped"] * 4
    replica.close()


def test_apply_supersede_update(store):
    ep, old = seed(store)
    new = add_draft(
        store, ep.id, draft("Everyone agreed"), valid_from=WHEN + timedelta(days=1)
    )
    envs_before = drain(store)
    supersede(store, old.id, new.id)
    envs_after = drain(store)

    replica = Store(":memory:")
    for e in envs_before + envs_after:
        assert not replica.apply_change(e).startswith("quarantined")
    assert replica.get_unit(old.id).valid_to == WHEN + timedelta(days=1)
    replica.close()


def test_concurrent_supersede_converges_on_min_valid_to(store):
    """Two devices close the same unit at different times — every replica
    must converge on the earliest close regardless of arrival order."""
    ep, unit = seed(store)
    base = drain(store)
    early = unit.model_copy(deep=True)
    early.valid_to = WHEN + timedelta(days=1)
    late = unit.model_copy(deep=True)
    late.valid_to = WHEN + timedelta(days=2)

    def update_env(u, seq, device):
        return envelope(
            {
                "change_id": f"chg_{device}{seq}",
                "table": "units",
                "op": "update",
                "row_id": u.id,
                "payload": json.loads(u.model_dump_json()),
                "recorded_at": "2026-07-20T00:00:00+00:00",
            },
            seq=seq,
            device=device,
        )

    for order in ([early, late], [late, early]):
        replica = Store(":memory:")
        for e in base:
            replica.apply_change(e)
        for i, u in enumerate(order):
            replica.apply_change(update_env(u, seq=10 + i, device="dev-x"))
        assert replica.get_unit(unit.id).valid_to == WHEN + timedelta(days=1)
        replica.close()


def test_alias_union_converges(store):
    entity = create_entity(store, "Priya")
    base = drain(store)
    a = entity.model_copy(deep=True)
    a.aliases = ["priya.k"]
    b = entity.model_copy(deep=True)
    b.aliases = ["pk-slack"]

    def update_env(ent, seq):
        return envelope(
            {
                "change_id": f"chg_al{seq}",
                "table": "entities",
                "op": "update",
                "row_id": ent.id,
                "payload": json.loads(ent.model_dump_json()),
                "recorded_at": "2026-07-20T00:00:00+00:00",
            },
            seq=seq,
        )

    for order in ([a, b], [b, a]):
        replica = Store(":memory:")
        for e in base:
            replica.apply_change(e)
        for i, ent in enumerate(order):
            replica.apply_change(update_env(ent, seq=10 + i))
        assert set(replica.get_entity(entity.id).aliases) == {"priya.k", "pk-slack"}
        replica.close()


def test_apply_episode_rename(store):
    ep, _ = seed(store)
    base = drain(store)
    store.rename_episode(ep.id, "Renamed thread")
    rename_envs = drain(store)

    replica = Store(":memory:")
    for e in base:
        replica.apply_change(e)
    assert replica.apply_change(rename_envs[0]) == "applied"
    got = replica.get_episode(ep.id)
    assert got.title == "Renamed thread"
    assert got.raw == RAW  # rename never touches raw
    # Re-apply is a no-op; apply never writes the replica's own outbox.
    assert replica.apply_change(rename_envs[0]) == "skipped"
    assert replica.pending_changes() == []
    replica.close()


def test_episode_rename_before_insert_quarantines_and_retries(store):
    ep, _ = seed(store)
    base = drain(store)
    store.rename_episode(ep.id, "Renamed thread")
    rename_envs = drain(store)

    replica = Store(":memory:")
    assert replica.apply_change(rename_envs[0]) == "quarantined:missing-row"
    assert replica.quarantine_count() == 1
    for e in base:
        replica.apply_change(e)
    assert replica.retry_quarantine() == 1
    assert replica.get_episode(ep.id).title == "Renamed thread"
    replica.close()


def test_concurrent_rename_is_last_applied_wins(store):
    """Episodes carry no update timestamp, so concurrent renames on two
    devices are last-applied-wins — documented v0 policy (NOT commutative)."""
    ep, _ = seed(store)
    base = drain(store)
    a = ep.model_copy(deep=True)
    a.title = "Title from device A"
    b = ep.model_copy(deep=True)
    b.title = "Title from device B"

    def update_env(episode, seq, device):
        return envelope(
            {
                "change_id": f"chg_{device}{seq}",
                "table": "episodes",
                "op": "update",
                "row_id": episode.id,
                "payload": json.loads(episode.model_dump_json()),
                "recorded_at": "2026-07-20T00:00:00+00:00",
            },
            seq=seq,
            device=device,
        )

    for order in ([a, b], [b, a]):
        replica = Store(":memory:")
        for e in base:
            replica.apply_change(e)
        for i, episode in enumerate(order):
            replica.apply_change(update_env(episode, seq=10 + i, device="dev-x"))
        assert replica.get_episode(ep.id).title == order[-1].title
        replica.close()


def test_gate_failure_quarantines(store):
    ep, unit = seed(store)
    envs = drain(store)
    # Tamper the unit's evidence so it is no longer a literal span of raw.
    envs[1]["payload"]["evidence"][0]["text"] = "something never said"

    replica = Store(":memory:")
    assert replica.apply_change(envs[0]) == "applied"
    assert replica.apply_change(envs[1]) == "quarantined:gate-failed"
    assert replica.quarantine_count() == 1
    with pytest.raises(Exception):
        replica.get_unit(unit.id)
    replica.close()


def test_missing_dependency_quarantine_and_retry(store):
    ep, unit = seed(store)
    entity = create_entity(store, "Priya")
    store.add_edge(unit.id, entity.id, kind="mentions")
    envs = drain(store)  # ep, unit, entity, edge

    replica = Store(":memory:")
    # Deliver wildly out of order: edge first (both endpoints missing),
    # then unit (episode missing).
    assert replica.apply_change(envs[3]) == "quarantined:missing-node"
    assert replica.apply_change(envs[1]) == "quarantined:missing-episode"
    assert replica.quarantine_count() == 2
    # Dependencies arrive.
    assert replica.apply_change(envs[0]) == "applied"
    assert replica.apply_change(envs[2]) == "applied"
    # The retry pass clears the quarantine.
    assert replica.retry_quarantine() == 2
    assert replica.quarantine_count() == 0
    assert replica.get_unit(unit.id).id == unit.id
    assert len(replica.edges_for(unit.id)) == 1
    replica.close()


def test_external_id_conflict_quarantines(store):
    from vetromar.schema import Episode

    def sourced_episode(title):
        return Episode(
            source_kind="chat",
            title=title,
            occurred_at=WHEN,
            raw="x",
            external_id="slack:C1:1",
        )

    store.add_episode(sourced_episode("A"))
    envs = drain(store)
    replica = Store(":memory:")
    # The replica already ingested the same source item as its own episode.
    replica.add_episode(sourced_episode("B"))
    assert replica.apply_change(envs[0]) == "quarantined:external-id-conflict"
    replica.close()


# -- migration ---------------------------------------------------------------


def test_v3_store_migrates_additively_to_v4(tmp_path):
    db = tmp_path / "store.db"
    store = Store(db)
    ep, unit = seed(store)
    # Rewind to a v3 store: drop the v4 tables and set the version stamp.
    store._conn.executescript(
        "DROP TABLE changelog; DROP TABLE replication_state;"
        " DROP TABLE replication_quarantine; PRAGMA user_version = 3;"
    )
    store._conn.commit()
    store.close()

    migrated = Store(db)
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == 4
    # Data intact, outbox empty (pre-existing rows are seeded by bootstrap,
    # not by migration).
    assert migrated.get_unit(unit.id).id == unit.id
    assert migrated.get_episode(ep.id).raw == RAW
    assert migrated.pending_changes() == []
    # And the new machinery works on the migrated store.
    entity = create_entity(migrated, "Priya")
    assert [c["row_id"] for c in migrated.pending_changes()] == [entity.id]
    migrated.close()
