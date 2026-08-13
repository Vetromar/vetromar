"""G4: contributor provenance + the membrane (push private → shared).

The evidence gate is the constant: everything that crosses the membrane
re-validates at the destination door and again on every member's replica."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vetromar import operations
from vetromar.ingest.manual import add_draft, add_source_episode, create_entity
from vetromar.ingest.notes import add_quick_note
from vetromar.schema import ClaimPayload, ContributorRef, Episode, ExcerptEvidence, UnitDraft
from vetromar.store import Store

WHEN = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
RAW = "Priya said the hinge should be brass, not steel. Everyone agreed."

LEO = ContributorRef(public_key="pk_leo", handle="leo", display_name="Leo")


def draft(text="the hinge should be brass"):
    return UnitDraft(
        content=f"Claim: {text}",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=text)],
    )


@pytest.fixture
def src():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def dst():
    s = Store(":memory:")
    s.contributor = LEO
    yield s
    s.close()


def seed(store):
    ep = add_source_episode(store, title="Thread", source_kind="chat", raw=RAW, occurred_at=WHEN)
    u1 = add_draft(store, ep.id, draft(), valid_from=WHEN)
    u2 = add_draft(store, ep.id, draft("Everyone agreed"), valid_from=WHEN)
    entity = create_entity(store, "Priya")
    store.add_edge(u1.id, u2.id, kind="related")
    store.add_edge(u1.id, entity.id, kind="mentions")
    return ep, u1, u2, entity


# -- contributor stamping ---------------------------------------------------------


def test_writes_into_contributor_store_are_stamped(dst):
    episode, unit = add_quick_note(dst, "brass hinge it is")
    assert dst.get_unit(unit.id).provenance.contributor == LEO
    assert dst.get_episode(episode.id).contributor == LEO


def test_private_store_stays_unstamped(src):
    episode, unit = add_quick_note(src, "just for me")
    assert src.get_unit(unit.id).provenance.contributor is None
    assert src.get_episode(episode.id).contributor is None


def test_apply_never_stamps_the_local_contributor(src, dst):
    """Replicated changes keep THEIR contributor (or none) — the local
    identity must never claim remote work."""
    add_quick_note(src, "someone else's note")
    for record in src.pending_changes(limit=10):
        result = dst.apply_change({**record, "seq": 1, "origin_device_id": "dev-x"})
        assert result == "applied"
    unit = dst.list_units()[0]
    assert unit.provenance.contributor is None


def test_old_payloads_without_contributor_validate():
    Episode.model_validate({"id": "ep_x", "source_kind": "note", "title": "t",
                            "occurred_at": "2026-08-01T00:00:00+00:00"})


# -- the membrane ------------------------------------------------------------------


def test_share_copies_episode_raw_and_units_through_the_gate(src, dst):
    ep, u1, u2, _ = seed(src)
    report = operations.share_to_graph(src, dst, unit_ids=[u1.id, u2.id])
    assert report["episodes_copied"] == 1
    assert report["units_copied"] == 2
    # IDs preserved; raw travels; the gate passed at the destination door.
    assert dst.get_episode(ep.id).raw == RAW
    assert dst.get_unit(u1.id).content == u1.content
    # The copies are stamped as the sharer's contribution.
    assert dst.get_unit(u1.id).provenance.contributor == LEO
    assert dst.get_episode(ep.id).contributor == LEO


def test_share_whole_episode(src, dst):
    ep, u1, u2, _ = seed(src)
    report = operations.share_to_graph(src, dst, episode_ids=[ep.id])
    assert report["units_copied"] == 2


def test_share_is_idempotent(src, dst):
    ep, u1, u2, _ = seed(src)
    operations.share_to_graph(src, dst, unit_ids=[u1.id, u2.id])
    report = operations.share_to_graph(src, dst, unit_ids=[u1.id, u2.id])
    assert report["units_copied"] == 0
    assert report["episodes_copied"] == 0
    assert len(dst.list_units()) == 2


def test_share_copies_inside_edges_drops_outside_ones(src, dst):
    ep, u1, u2, entity = seed(src)
    report = operations.share_to_graph(src, dst, unit_ids=[u1.id, u2.id])
    kinds = {e.kind for e in dst.edges_for(u1.id)}
    assert "related" in kinds  # u1—u2: both copied
    assert "mentions" not in kinds  # u1—entity: entity stays home
    assert report["edges_copied"] == 1
    assert report["edges_dropped"] == 1
    # Entities are never copied — the shared graph builds its own layer.
    assert dst.list_entities() == []
    # Re-share doesn't duplicate the edge.
    operations.share_to_graph(src, dst, unit_ids=[u1.id, u2.id])
    assert len([e for e in dst.edges_for(u1.id) if e.kind == "related"]) == 1


def test_share_superseded_history_travels(src, dst):
    from vetromar.ingest.manual import supersede

    ep, u1, u2, _ = seed(src)
    supersede(src, u1.id, u2.id)
    operations.share_to_graph(src, dst, unit_ids=[u1.id, u2.id])
    assert dst.get_unit(u1.id).valid_to is not None


def test_shared_copies_survive_replication_with_zero_quarantine(src, dst):
    """The full journey: private → shared replica → (sync wire) → another
    member's replica. The gate re-runs at every door."""
    ep, u1, u2, _ = seed(src)
    operations.share_to_graph(src, dst, unit_ids=[u1.id, u2.id])

    other = Store(":memory:")
    seq = 0
    for record in dst.pending_changes(limit=50):
        seq += 1
        result = other.apply_change({**record, "seq": seq, "origin_device_id": "dev-b"})
        assert result == "applied", (result, record["table"])
    assert other.quarantine_count() == 0
    got = other.get_unit(u1.id)
    assert got.provenance.contributor == LEO  # provenance survived the wire
    assert other.get_episode(ep.id).contributor == LEO
    other.close()
