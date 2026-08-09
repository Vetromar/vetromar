"""Store v2: provenance round-trips, the evidence door, bi-temporal
supersession, entities, and typed edges."""

from datetime import datetime, timezone

import pytest

from tests.conftest import make_billing_unit
from vetromar.ingest import ingest_room
from vetromar.ingest.manual import (
    add_draft,
    add_source_episode,
    add_unit,
    create_entity,
    link_alias,
    link_units,
    supersede,
)
from vetromar.schema import (
    ClaimPayload,
    ExcerptEvidence,
    ExtractedUnit,
    PersonRef,
    Status,
    UnitDraft,
)
from vetromar.store import StoreError

WHEN = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _room(store, units=None, title="Architecture review"):
    return ingest_room(store, units or [make_billing_unit()], title=title, occurred_at=WHEN)


def _ticket_draft(text="Ticket: carve invoicing service out of monolith"):
    """A concierge-style claim with excerpt evidence (source episodes in these
    tests carry no raw, so the gate checks presence, not literality)."""
    return UnitDraft(
        content=text,
        reasoning="Spawned by the architecture review decision",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=text, author=PersonRef(ref="priya.k"))],
    )


def test_room_ingest_round_trip(store):
    episode, units = _room(store)
    assert episode.source_kind == "meeting"

    got = store.get_unit(units[0].id)
    assert got.type == "decision"
    assert got.content == units[0].content
    assert got.content.startswith("Move billing off the monolith")
    assert got.provenance.episode_id == episode.id
    assert got.provenance.method == "captured"
    assert got.payload.status == Status.DECIDED
    assert got.payload.advocate.ref == "SPEAKER_02"
    assert got.evidence[0].kind == "quote"
    assert got.evidence[0].start_ms == 70400
    assert got.valid_from == WHEN
    assert got.valid_to is None
    # provenance chain resolves back to the episode
    assert store.get_episode(got.provenance.episode_id).title == "Architecture review"


def test_episode_raw_round_trips(store):
    ep = add_source_episode(
        store, title="Email thread", source_kind="email_thread", raw="We agreed to ship v2."
    )
    assert store.get_episode(ep.id).raw == "We agreed to ship v2."
    assert store.get_episode(ep.id).source_kind == "email_thread"


def test_rename_episode(store):
    ep = add_source_episode(
        store, title="Meeting 2026-07-21 14:30", source_kind="meeting", raw="We agreed."
    )
    renamed = store.rename_episode(ep.id, "Q3 planning kickoff")
    assert renamed.title == "Q3 planning kickoff"
    got = store.get_episode(ep.id)
    assert got.title == "Q3 planning kickoff"
    assert got.raw == "We agreed."  # only the title moved


def test_rename_unknown_episode_raises(store):
    with pytest.raises(StoreError):
        store.rename_episode("ep_nope", "anything")


def test_unit_requires_real_episode(store):
    from vetromar.ingest.map import unit_from_extracted

    dangling = unit_from_extracted(
        make_billing_unit(), episode_id="ep_nope", method="captured", valid_from=WHEN
    )
    with pytest.raises(StoreError):
        store.add_unit(dangling)


def test_source_unit_is_same_record_type(store):
    """A hand-entered source unit and a room unit differ ONLY by provenance."""
    ep = add_source_episode(store, title="JIRA BILL-142", raw_ref="https://jira/BILL-142")
    unit = add_unit(store, ep.id, make_billing_unit())
    got = store.get_unit(unit.id)
    assert type(got) is type(_room_unit := _room(store)[1][0])
    assert got.provenance.method == "concierge"
    assert _room_unit.provenance.method == "captured"


def test_bitemporal_supersede(store):
    episode, (old,) = _room(store)
    new_extracted = ExtractedUnit(
        decision="Keep residency flags; delete the rest",
        reasoning="Compliance: three flags gate the EU data residency path",
        status=Status.DECIDED,
        grounded_quotes=[
            # source episode has no raw in this test; presence is what's gated
        ],
    )
    later = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)
    ep2 = add_source_episode(store, title="Standup", occurred_at=later)
    new = add_draft(store, ep2.id, _ticket_draft("Keep residency flags; delete the rest"), valid_from=later)

    closed = supersede(store, old.id, new.id)
    assert closed.valid_to == later
    # history preserved — the old unit still exists, just bounded in validity
    assert store.get_unit(old.id).content == old.content
    # current_only excludes the superseded one
    current = store.list_units(current_only=True)
    assert [u.id for u in current] == [new.id]
    # the reversal is also carried as a supersedes edge (new -> old)
    edge = store.edges_for(old.id, kind="supersedes")[0]
    assert (edge.from_id, edge.to_id, edge.method) == (new.id, old.id, "manual")
    # double-supersede is an error, not a silent overwrite
    with pytest.raises(StoreError):
        supersede(store, old.id, new.id)


def test_as_of_valid_time_axis(store):
    _, (old,) = _room(store)  # valid_from July 1
    later = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)
    ep2 = add_source_episode(store, title="Standup", occurred_at=later)
    new = add_draft(store, ep2.id, _ticket_draft("The reversal"), valid_from=later)
    supersede(store, old.id, new.id)

    july5 = datetime(2026, 7, 5, tzinfo=timezone.utc)
    july9 = datetime(2026, 7, 9, tzinfo=timezone.utc)
    assert [u.id for u in store.list_units(as_of=july5)] == [old.id]
    assert [u.id for u in store.list_units(as_of=july9)] == [new.id]


def test_entity_linking_and_units_by_entity(store):
    _, (unit,) = _room(store)
    priya = create_entity(store, "Priya")
    link_alias(store, priya.id, "SPEAKER_02")   # room label
    link_alias(store, priya.id, "priya.k")      # slack handle

    assert set(store.get_entity(priya.id).aliases) == {"SPEAKER_02", "priya.k"}
    assert store.resolve_alias("SPEAKER_02").name == "Priya"
    assert store.resolve_alias(" priya ").name == "Priya"  # casefold/strip tier
    assert store.resolve_alias("nobody") is None

    found = store.units_by_entity(priya.id)
    assert [u.id for u in found] == [unit.id]  # she's the advocate


def test_edges_fusion_and_dedup(store):
    _, (room_unit,) = _room(store)
    ep = add_source_episode(store, title="JIRA BILL-142")
    source_unit = add_draft(store, ep.id, _ticket_draft())

    edge = link_units(store, room_unit.id, source_unit.id, kind="spawned")
    assert edge.kind == "spawned"
    assert edge.method == "manual"
    assert edge.confidence is None
    # visible from both ends
    assert {e.to_id for e in store.edges_for(room_unit.id)} == {source_unit.id}
    assert {e.from_id for e in store.edges_for(source_unit.id)} == {room_unit.id}
    # a duplicate (from, to, kind) returns the stored edge, not a phantom
    again = link_units(store, room_unit.id, source_unit.id, kind="spawned")
    assert again.id == edge.id
    # linking a nonexistent node fails loudly
    with pytest.raises(StoreError):
        link_units(store, room_unit.id, "unit_nope")
    with pytest.raises(StoreError):
        store.add_edge(room_unit.id, "banana")
    # list_edges returns every stored edge
    store.add_edge(source_unit.id, room_unit.id, kind="related")
    assert {(e.from_id, e.to_id, e.kind) for e in store.list_edges()} == {
        (room_unit.id, source_unit.id, "spawned"),
        (source_unit.id, room_unit.id, "related"),
    }


def test_unit_entity_edges(store):
    _, (unit,) = _room(store)
    priya = create_entity(store, "Priya")
    edge = store.add_edge(
        unit.id, priya.id, kind="mentions",
        method="auto-exact", confidence=1.0, ref="SPEAKER_02",
    )
    assert edge.confidence == 1.0
    assert store.edges_for(priya.id)[0].ref == "SPEAKER_02"


def test_list_units_filters(store):
    episode, _ = _room(store)
    ep = add_source_episode(store, title="Slack thread")
    add_draft(store, ep.id, _ticket_draft("Keep Stripe for now"))

    assert len(store.list_units()) == 2
    assert len(store.list_units(type="decision")) == 1
    assert len(store.list_units(type="claim")) == 1
    assert len(store.list_units(status="Decided")) == 1
    assert len(store.list_units(method="captured")) == 1
    assert len(store.list_units(method="concierge")) == 1
    assert len(store.list_units(episode_id=episode.id)) == 1


def test_fts_search_ranks_and_survives_hostile_queries(store):
    _room(store)
    ep = add_source_episode(store, title="Slack thread")
    add_draft(store, ep.id, _ticket_draft("Keep Stripe for now"))

    hits = store.search_fts("monolith invoicing")
    assert hits and hits[0][0].content.startswith("Move billing off the monolith")
    assert [u.content for u, _ in store.search_fts("Stripe")] == ["Keep Stripe for now"]
    # raw agent text must never crash MATCH
    for hostile in ("re-architecture", "don't", "trailing OR", '"unbalanced', "-", "   "):
        store.search_fts(hostile)


def test_list_pagination(store):
    ep = add_source_episode(store, title="Thread")
    for i in range(5):
        add_draft(store, ep.id, _ticket_draft(f"Claim number {i}"))
    for i in range(3):
        add_source_episode(store, title=f"Thread {i}")
    create_entity(store, "Priya K")
    create_entity(store, "Sam")

    all_units = store.list_units()
    assert [u.id for u in store.list_units(limit=2, offset=1)] == [
        u.id for u in all_units[1:3]
    ]
    assert len(store.list_episodes(limit=2)) == 2
    assert len(store.list_episodes(limit=10, offset=3)) == 1
    assert [e.name for e in store.list_entities(limit=1, offset=1)] == ["Sam"]


def test_v4_store_migrates_additively_to_v5(tmp_path):
    from vetromar.store import Store
    from vetromar.store.store import SCHEMA_VERSION

    db = tmp_path / "store.db"
    store = Store(db)
    ep = add_source_episode(store, title="Thread")
    unit = add_draft(store, ep.id, _ticket_draft("Keep Stripe for now"))
    entity = create_entity(store, "Priya K")
    link_alias(store, entity.id, "priya.k")
    # Rewind to a v4 store: drop the v5 derived tables and stamp the version.
    store._conn.executescript(
        "DROP TABLE embed_pending; DROP TABLE entity_aliases;"
        " DROP TABLE unit_refs; PRAGMA user_version = 4;"
    )
    store._conn.commit()
    store.close()

    migrated = Store(db)
    assert (
        migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    )
    # entity_aliases backfilled: both tiers of resolve_alias work off SQL rows
    assert migrated.resolve_alias("priya.k").id == entity.id
    assert migrated.resolve_alias("PRIYA K").id == entity.id
    # unit_refs backfilled: ref-only association still found (no edges exist)
    assert [u.id for u in migrated.units_by_entity(entity.id)] == [unit.id]
    # embed_pending backfilled: the unembedded unit is queued for the search layer
    assert migrated.has_pending_embeddings()
    assert [u.id for u in migrated.units_pending_embedding()] == [unit.id]
    migrated.close()


# -- temporal edges (v6) -----------------------------------------------------


def test_invalidate_edge_round_trip(store):
    ep = add_source_episode(store, title="Thread")
    a = add_draft(store, ep.id, _ticket_draft("Claim A"))
    b = add_draft(store, ep.id, _ticket_draft("Claim B"))
    edge = store.add_edge(a.id, b.id, kind="related")
    assert edge.valid_to is None

    closed = store.invalidate_edge(edge.id, at=WHEN)
    assert closed.valid_to == WHEN
    # history preserved, filtered views work
    assert store.edges_for(a.id, kind="related") != []
    assert store.edges_for(a.id, kind="related", current_only=True) == []
    before = WHEN.replace(hour=9)
    assert store.edges_for(a.id, as_of=before) == []  # edge didn't exist yet then
    with pytest.raises(StoreError):
        store.invalidate_edge(edge.id)  # already closed
    with pytest.raises(StoreError):
        store.invalidate_edge("edge_nope")


def test_supersede_closes_old_units_open_edges(store):
    ep = add_source_episode(store, title="Thread")
    old = add_draft(store, ep.id, _ticket_draft("Old direction"))
    other = add_draft(store, ep.id, _ticket_draft("Bystander"))
    entity = create_entity(store, "Priya K")
    store.add_edge(old.id, entity.id, kind="mentions")
    store.add_edge(old.id, other.id, kind="related")
    new = add_draft(store, ep.id, _ticket_draft("New direction"))
    supersedes_edge = store.add_edge(new.id, old.id, kind="supersedes")

    closed = supersede(store, old.id, new.id)
    # the old unit's own edges closed at the supersede instant...
    for edge in store.edges_for(entity.id) + store.edges_for(other.id):
        assert edge.valid_to == closed.valid_to
    # ...but the new unit's supersedes assertion stays open
    assert store.edges_for(new.id, kind="supersedes")[0].valid_to is None
    assert supersedes_edge.id == store.edges_for(new.id, kind="supersedes")[0].id


def test_entity_profile_and_merge_redirect(store):
    a = create_entity(store, "Priya K")
    b = create_entity(store, "priya.k")
    store.update_entity_profile(a.id, summary="Payments lead", attributes={"team": "billing"})
    store.update_entity_profile(b.id, merged_into=a.id)

    got = store.get_entity(a.id)
    assert got.summary == "Payments lead"
    assert got.attributes == {"team": "billing"}
    # reads follow the redirect
    assert store.resolve_entity(b.id).id == a.id
    # merged entities are hidden by default, listable on request
    assert [e.id for e in store.list_entities()] == [a.id]
    assert len(store.list_entities(include_merged=True)) == 2
