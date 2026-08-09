"""Store v3: the v2->v3 additive migration, episode external_id idempotency
(deliver_episode dedup + revision semantics), and per-source sync state."""

import sqlite3
from datetime import datetime, timezone

import pytest

from vetromar.errors import ConfigError
from vetromar.sources import deliver_episode
from vetromar.store import Store, StoreError

WHEN = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)

# The episodes table exactly as v2 shipped it (no external_id).
_V2_EPISODES_DDL = """
CREATE TABLE episodes (
    id          TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    title       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    raw         TEXT,
    raw_ref     TEXT
);
"""


def _make_v2_store(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(_V2_EPISODES_DDL)
    conn.execute(
        "INSERT INTO episodes (id, source_kind, title, occurred_at, ingested_at, raw)"
        " VALUES ('ep_old', 'chat', 'Old thread', ?, ?, 'alpha said hello')",
        (WHEN.isoformat(), WHEN.isoformat()),
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


# -- migration ---------------------------------------------------------------


def test_v2_store_migrates_additively_to_current(tmp_path):
    from vetromar.store.store import SCHEMA_VERSION

    db = tmp_path / "store.db"
    _make_v2_store(db)

    store = Store(db)
    assert (
        store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    )
    # pre-existing data survives, with external_id defaulting to NULL
    old = store.get_episode("ep_old")
    assert old.title == "Old thread"
    assert old.external_id is None
    # the new surfaces work on the migrated store
    result = deliver_episode(
        store, title="New", source_kind="chat", raw="beta said hi",
        external_id="slack:C1:1", occurred_at=WHEN,
    )
    assert result.created
    store.set_sync_state("slack", '{"ts": "1"}')
    assert store.get_sync_state("slack")[0] == '{"ts": "1"}'
    store.close()

    # reopen: migration is a one-time event, everything still there
    store = Store(db)
    assert store.get_episode_by_external_id("slack:C1:1").title == "New"
    store.close()


def test_pre_v2_store_still_gets_delete_hint(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE units (id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(ConfigError):
        Store(db)


# -- external_id idempotency -------------------------------------------------


def _deliver(store, raw="alice: ship it\nbob: agreed", external_id="slack:C9:100.1"):
    return deliver_episode(
        store, title="#eng thread", source_kind="chat",
        raw=raw, external_id=external_id, occurred_at=WHEN,
    )


def test_first_delivery_creates(store):
    result = _deliver(store)
    assert result.created
    assert result.episode.external_id == "slack:C9:100.1"
    assert store.get_episode_by_external_id("slack:C9:100.1").raw.startswith("alice")


def test_identical_redelivery_is_a_noop(store):
    first = _deliver(store)
    again = _deliver(store)
    assert not again.created
    assert again.episode.id == first.episode.id
    assert len(store.list_episodes()) == 1


def test_whitespace_variants_dedupe(store):
    _deliver(store, raw="alice: ship it\nbob: agreed")
    again = _deliver(store, raw="alice: ship it   \n\n bob: agreed")
    assert not again.created


def test_changed_content_lands_as_revision(store):
    first = _deliver(store)
    grown = _deliver(store, raw="alice: ship it\nbob: agreed\ncarol: wait, no")
    assert grown.created
    assert grown.episode.id != first.episode.id
    assert grown.episode.external_id.startswith("slack:C9:100.1@")
    # the original is untouched — history never mutated
    assert store.get_episode(first.episode.id).raw == "alice: ship it\nbob: agreed"
    # re-delivering the grown content dedupes against the revision
    again = _deliver(store, raw="alice: ship it\nbob: agreed\ncarol: wait, no")
    assert not again.created
    assert again.episode.id == grown.episode.id


def test_unique_index_backstop(store):
    _deliver(store)
    from vetromar.ingest.generic import ingest_episode

    with pytest.raises(StoreError):
        ingest_episode(
            store, title="dup", source_kind="chat",
            raw="different", external_id="slack:C9:100.1",
        )


# -- sync state --------------------------------------------------------------


def test_sync_state_roundtrip(store):
    assert store.get_sync_state("slack") is None
    store.set_sync_state("slack", '{"C9": "100.1"}')
    cursor, synced_at = store.get_sync_state("slack")
    assert cursor == '{"C9": "100.1"}'
    assert synced_at  # ISO timestamp
    store.set_sync_state("slack", '{"C9": "200.5"}')
    assert store.get_sync_state("slack")[0] == '{"C9": "200.5"}'
