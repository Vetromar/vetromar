"""First-sign-in outbox seeding: rows written before replication existed

(any store predating replication) get insert change records so
the whole store uploads on day one. Idempotent: only rows with no insert
entry in the changelog are seeded, and a replication_state flag skips the
scan entirely once done. Dependency order (episodes → units → entities →
edges) so seq order at the server preserves apply causality.
"""

from __future__ import annotations

import logging

from vetromar.store import Store

logger = logging.getLogger("vetromar.workspace.sync")

SEEDED_KEY = "bootstrap_seeded"


def ensure_seeded(store: Store) -> int:
    if store.get_replication_state(SEEDED_KEY):
        return 0
    covered = store.changelog_covered()
    seeded = 0

    def seed(table: str, row_id: str, payload_json: str) -> None:
        nonlocal seeded
        if (table, row_id) in covered:
            return
        store.record_change(table, "insert", row_id, payload_json)
        seeded += 1

    for episode in store.list_episodes():
        seed("episodes", episode.id, episode.model_dump_json())
    for unit in store.list_units():
        # Superseded units seed as inserts too — the payload carries valid_to.
        seed("units", unit.id, unit.model_dump_json())
    for entity in store.list_entities():
        seed("entities", entity.id, entity.model_dump_json())
    for edge in store.list_edges():
        seed("edges", edge.id, edge.model_dump_json())

    store.set_replication_state(SEEDED_KEY, "1")
    if seeded:
        logger.info("bootstrap: seeded %d pre-existing rows for upload", seeded)
    return seeded
