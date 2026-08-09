"""The store — the platform's spine.

A thin graph-shaped store over SQLite: typed unit nodes + entity nodes +
typed edges + episodes as the raw layer. Deliberately still SQLite (not a
graph engine): the workload is hybrid retrieval + shallow traversal, and the
store must run local-first on a future in-room device. The data model is
explicitly nodes-and-edges so a graph engine can be swapped in later if real
workloads demand it.

Rules:
- Inserts never mutate history. A reversal calls `supersede(old_id, new_id)`,
  which closes `valid_to` on the prior unit; `ingested_at` is immutable.
- HARD INVARIANT, enforced at this door: every unit carries >=1 evidence item,
  and textual evidence must be a literal span of its episode's raw content
  (see extraction/validate.validate_unit_evidence).
- `payload` columns are the source of truth; typed columns are denormalized
  copies for filtering. The `units_fts` FTS5 table is maintained on insert.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from vetromar.errors import ConfigError
from vetromar.extraction.validate import ExtractionGateError, validate_unit_evidence
from vetromar.schema import Edge, Entity, Episode, Unit
from vetromar.workspace.wire import new_change_id

logger = logging.getLogger("vetromar.store.replication")

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()

SCHEMA_VERSION = 4

# Additive-only migrations from versions that shipped with real customer data.
# v2 was the first schema real stores ran on, so v2 -> v3 migrates in place;
# pre-v2 stores still get the delete-hint (they never held data worth keeping).
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE episodes ADD COLUMN external_id TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_external"
        " ON episodes(external_id) WHERE external_id IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS sync_state ("
        " source TEXT PRIMARY KEY, cursor TEXT NOT NULL, last_synced_at TEXT NOT NULL)",
    ),
    # v3 -> v4 (M14): workspace replication bookkeeping. Pure additions; the
    # CREATEs match schema.sql exactly.
    3: (
        "CREATE TABLE IF NOT EXISTS changelog ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT NOT NULL UNIQUE,"
        " table_name TEXT NOT NULL, op TEXT NOT NULL, row_id TEXT NOT NULL,"
        " payload TEXT NOT NULL, recorded_at TEXT NOT NULL,"
        " pushed INTEGER NOT NULL DEFAULT 0)",
        "CREATE INDEX IF NOT EXISTS idx_changelog_pending ON changelog(pushed, seq)",
        "CREATE TABLE IF NOT EXISTS replication_state ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS replication_quarantine ("
        " change_id TEXT PRIMARY KEY, envelope TEXT NOT NULL, reason TEXT NOT NULL,"
        " received_at TEXT NOT NULL)",
    ),
}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class StoreError(Exception):
    pass


def unit_refs(unit: Unit) -> set[str]:
    """Every PersonRef string a unit carries (payload roles + evidence
    speakers/authors) — the raw material for entity resolution."""
    refs: set[str] = set()
    payload = unit.payload
    for role in ("advocate", "owner", "raised_by"):
        person = getattr(payload, role, None)
        if person is not None:
            refs.add(person.ref)
    for objection in getattr(payload, "objectors", []):
        refs.add(objection.person.ref)
    for ev in unit.evidence:
        who = getattr(ev, "speaker", None) or getattr(ev, "author", None)
        if who is not None:
            refs.add(who.ref)
    return refs


def fts_text(unit: Unit) -> str:
    """The searchable text of a unit: content + reasoning + payload prose +
    evidence text + ref strings. Also the passage text the search layer
    embeds — one derivation, two indexes."""
    parts = [unit.content]
    if unit.reasoning:
        parts.append(unit.reasoning)
    payload = unit.payload
    if payload.kind == "decision":
        for alt in payload.rejected_alternatives:
            parts.extend((alt.alternative, alt.why_rejected))
        for objection in payload.objectors:
            parts.append(objection.grounds)
    elif payload.kind == "metric":
        parts.append(payload.metric)
        if payload.source_system:
            parts.append(payload.source_system)
    for ev in unit.evidence:
        if ev.kind in ("quote", "excerpt"):
            parts.append(ev.text)
        else:
            parts.append(ev.description)
    parts.extend(sorted(unit_refs(unit)))
    return "\n".join(parts)


def _fts_match_expr(query: str) -> Optional[str]:
    """Sanitize free text into an FTS5 MATCH expression. Raw text crashes
    MATCH ('re-architecture', "don't", trailing operators), so every
    whitespace token becomes a quoted string (implicit AND)."""
    tokens = [t.replace('"', '""') for t in query.split()]
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


class Store:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._check_schema_version(db_path)
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        self.vec_available = self._load_sqlite_vec()
        # Workspace replication: every knowledge write appends to the
        # changelog outbox unless we're applying a remote change.
        self._log_changes = True

    def _load_sqlite_vec(self) -> bool:
        # Optional acceleration: cosine top-k in SQL. Any failure (extension
        # loading disabled, missing wheel) degrades to the numpy scan — never
        # a hard requirement, so customer machines can't be bricked by it.
        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            try:
                sqlite_vec.load(self._conn)
            finally:
                self._conn.enable_load_extension(False)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _check_schema_version(self, db_path: Path) -> None:
        # Versions with real data migrate additively (_MIGRATIONS); anything
        # older gets a clear error instead of silent corruption.
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = (
            self._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
            > 0
        )
        if not has_tables or version >= SCHEMA_VERSION:
            return
        while version in _MIGRATIONS:
            for stmt in _MIGRATIONS[version]:
                self._conn.execute(stmt)
            version += 1
            self._conn.execute(f"PRAGMA user_version = {version}")
            self._conn.commit()
        if version < SCHEMA_VERSION:
            raise ConfigError(
                f"The store at {db_path} uses an old schema (v{version}, need v{SCHEMA_VERSION}).",
                hint=f"Pre-v2 stores aren't migrated — delete {db_path} and re-capture/re-ingest.",
            )

    def close(self) -> None:
        self._conn.close()

    def _record_change(self, table: str, op: str, row_id: str, payload_json: str) -> None:
        """Append to the replication outbox — same transaction as the write."""
        if not self._log_changes:
            return
        self._conn.execute(
            "INSERT INTO changelog (change_id, table_name, op, row_id, payload, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_change_id(),
                table,
                op,
                row_id,
                payload_json,
                _iso(datetime.now(timezone.utc)),
            ),
        )

    # -- episodes (the raw layer) --------------------------------------------

    def add_episode(self, episode: Episode) -> Episode:
        try:
            self._conn.execute(
                "INSERT INTO episodes (id, source_kind, title, occurred_at, ingested_at, raw, raw_ref, external_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode.id,
                    episode.source_kind,
                    episode.title,
                    _iso(episode.occurred_at),
                    _iso(episode.ingested_at),
                    episode.raw,
                    episode.raw_ref,
                    episode.external_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreError(
                f"episode with external_id {episode.external_id!r} already exists"
            ) from exc
        self._record_change("episodes", "insert", episode.id, episode.model_dump_json())
        self._conn.commit()
        return episode

    def rename_episode(self, episode_id: str, title: str) -> Episode:
        """Update an episode's title. The only mutable episode field — raw
        content, timing, and identity are immutable history."""
        episode = self.get_episode(episode_id)
        episode.title = title
        self._conn.execute(
            "UPDATE episodes SET title = ? WHERE id = ?", (title, episode_id)
        )
        self._record_change("episodes", "update", episode.id, episode.model_dump_json())
        self._conn.commit()
        return episode

    @staticmethod
    def _episode_from_row(row: sqlite3.Row) -> Episode:
        return Episode(
            id=row["id"],
            source_kind=row["source_kind"],
            title=row["title"],
            occurred_at=row["occurred_at"],
            ingested_at=row["ingested_at"],
            raw=row["raw"],
            raw_ref=row["raw_ref"],
            external_id=row["external_id"],
        )

    def get_episode(self, episode_id: str) -> Episode:
        row = self._conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"episode not found: {episode_id}")
        return self._episode_from_row(row)

    def get_episode_by_external_id(self, external_id: str) -> Optional[Episode]:
        row = self._conn.execute(
            "SELECT * FROM episodes WHERE external_id = ?", (external_id,)
        ).fetchone()
        return None if row is None else self._episode_from_row(row)

    def list_episodes(self) -> list[Episode]:
        rows = self._conn.execute(
            "SELECT id FROM episodes ORDER BY occurred_at"
        ).fetchall()
        return [self.get_episode(r["id"]) for r in rows]

    # -- units ---------------------------------------------------------------

    def add_unit(self, unit: Unit) -> Unit:
        # Provenance must point at a real episode — half the moat is that an
        # agent's answer is traceable; a dangling episode_id breaks that. The
        # same fetch feeds the evidence gate: the OTHER half of the moat is
        # that every unit is verifiable against its episode's raw content.
        episode = self.get_episode(unit.provenance.episode_id)
        validate_unit_evidence(unit, episode)
        try:
            self._insert_unit(unit)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return unit

    def add_units(self, units: list[Unit]) -> list[Unit]:
        """Atomic batch: every unit is gate-validated first; then all rows land
        in one transaction. A single gate failure rejects the whole batch."""
        for unit in units:
            episode = self.get_episode(unit.provenance.episode_id)
            validate_unit_evidence(unit, episode)
        try:
            for unit in units:
                self._insert_unit(unit)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return units

    def _insert_unit(self, unit: Unit) -> None:
        status = getattr(unit.payload, "status", None)
        self._conn.execute(
            "INSERT INTO units (id, type, status, episode_id, method,"
            " valid_from, valid_to, ingested_at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                unit.id,
                unit.type,
                status.value if status is not None else None,
                unit.provenance.episode_id,
                unit.provenance.method,
                _iso(unit.valid_from),
                _iso(unit.valid_to) if unit.valid_to else None,
                _iso(unit.ingested_at),
                unit.model_dump_json(),
            ),
        )
        self._conn.execute(
            "INSERT INTO units_fts (unit_id, text) VALUES (?, ?)",
            (unit.id, fts_text(unit)),
        )
        self._record_change("units", "insert", unit.id, unit.model_dump_json())

    def get_unit(self, unit_id: str) -> Unit:
        row = self._conn.execute(
            "SELECT payload FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"unit not found: {unit_id}")
        return Unit.model_validate_json(row["payload"])

    def list_units(
        self,
        type: Optional[str] = None,
        status: Optional[str] = None,
        episode_id: Optional[str] = None,
        method: Optional[str] = None,
        current_only: bool = False,
        as_of: Optional[datetime] = None,
    ) -> list[Unit]:
        sql = "SELECT payload FROM units WHERE 1=1"
        args: list = []
        if type:
            sql += " AND type = ?"
            args.append(type)
        if status:
            sql += " AND status = ?"
            args.append(status)
        if episode_id:
            sql += " AND episode_id = ?"
            args.append(episode_id)
        if method:
            sql += " AND method = ?"
            args.append(method)
        if current_only:
            sql += " AND valid_to IS NULL"
        if as_of is not None:
            # The valid-time axis: what held in the world at that moment.
            sql += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
            args.extend([_iso(as_of), _iso(as_of)])
        sql += " ORDER BY ingested_at"
        rows = self._conn.execute(sql, args).fetchall()
        return [Unit.model_validate_json(r["payload"]) for r in rows]

    def supersede(self, old_id: str, new_id: str) -> Unit:
        """Close validity on `old_id` as of the new unit's `valid_from`.
        History is never mutated — the old unit stays, bounded in validity."""
        old = self.get_unit(old_id)
        new = self.get_unit(new_id)
        if old.valid_to is not None:
            raise StoreError(f"unit already superseded: {old_id}")
        old.valid_to = new.valid_from
        self._conn.execute(
            "UPDATE units SET valid_to = ?, payload = ? WHERE id = ?",
            (_iso(old.valid_to), old.model_dump_json(), old_id),
        )
        self._record_change("units", "update", old_id, old.model_dump_json())
        self._conn.commit()
        return old

    # -- full-text search ----------------------------------------------------

    def search_fts(self, query: str, k: int = 20) -> list[tuple[Unit, float]]:
        """BM25-ranked full-text hits: (unit, score), best first. SQLite bm25
        is lower-is-better; callers should treat score as opaque rank input."""
        match = _fts_match_expr(query)
        if match is None:
            return []
        rows = self._conn.execute(
            "SELECT u.payload AS payload, bm25(units_fts) AS score"
            " FROM units_fts JOIN units u ON u.id = units_fts.unit_id"
            " WHERE units_fts MATCH ?"
            " ORDER BY bm25(units_fts)"
            " LIMIT ?",
            (match, k),
        ).fetchall()
        return [(Unit.model_validate_json(r["payload"]), r["score"]) for r in rows]

    # -- embeddings (written by the search layer) ----------------------------

    def put_embedding(self, unit_id: str, embedding: bytes) -> None:
        self._conn.execute(
            "INSERT INTO unit_vectors (unit_id, embedding) VALUES (?, ?)"
            " ON CONFLICT(unit_id) DO UPDATE SET embedding = excluded.embedding",
            (unit_id, embedding),
        )
        self._conn.commit()

    def get_embedding(self, unit_id: str) -> Optional[bytes]:
        row = self._conn.execute(
            "SELECT embedding FROM unit_vectors WHERE unit_id = ?", (unit_id,)
        ).fetchone()
        return row["embedding"] if row else None

    def list_embeddings(self) -> list[tuple[str, bytes]]:
        rows = self._conn.execute(
            "SELECT unit_id, embedding FROM unit_vectors"
        ).fetchall()
        return [(r["unit_id"], r["embedding"]) for r in rows]

    def units_missing_embedding(self) -> list[Unit]:
        rows = self._conn.execute(
            "SELECT u.payload AS payload FROM units u"
            " LEFT JOIN unit_vectors v ON v.unit_id = u.id"
            " WHERE v.unit_id IS NULL"
        ).fetchall()
        return [Unit.model_validate_json(r["payload"]) for r in rows]

    def vector_topk(self, query: bytes, n: int = 50) -> list[tuple[str, float]]:
        """Nearest units by cosine DISTANCE (lower = closer): (unit_id, dist).
        sqlite-vec in SQL when loadable, numpy scan otherwise — same results."""
        if self.vec_available:
            rows = self._conn.execute(
                "SELECT unit_id, vec_distance_cosine(embedding, ?) AS dist"
                " FROM unit_vectors ORDER BY dist LIMIT ?",
                (query, n),
            ).fetchall()
            return [(r["unit_id"], r["dist"]) for r in rows]
        return self._vector_topk_python(query, n)

    def _vector_topk_python(self, query: bytes, n: int) -> list[tuple[str, float]]:
        import numpy as np

        rows = self.list_embeddings()
        if not rows:
            return []
        q = np.frombuffer(query, dtype=np.float32)
        ids = [unit_id for unit_id, _ in rows]
        mat = np.stack([np.frombuffer(blob, dtype=np.float32) for _, blob in rows])
        qn = q / max(float(np.linalg.norm(q)), 1e-9)
        mn = mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9, None)
        sims = mn @ qn
        order = np.argsort(-sims)[:n]
        return [(ids[i], float(1.0 - sims[i])) for i in order]

    # -- entities ------------------------------------------------------------

    def add_entity(self, entity: Entity) -> Entity:
        self._conn.execute(
            "INSERT INTO entities (id, type, name, payload) VALUES (?, ?, ?, ?)",
            (entity.id, entity.type, entity.name, entity.model_dump_json()),
        )
        self._record_change("entities", "insert", entity.id, entity.model_dump_json())
        self._conn.commit()
        return entity

    def get_entity(self, entity_id: str) -> Entity:
        row = self._conn.execute(
            "SELECT payload FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"entity not found: {entity_id}")
        return Entity.model_validate_json(row["payload"])

    def list_entities(self, type: Optional[str] = None) -> list[Entity]:
        sql = "SELECT payload FROM entities"
        args: list = []
        if type:
            sql += " WHERE type = ?"
            args.append(type)
        sql += " ORDER BY name"
        rows = self._conn.execute(sql, args).fetchall()
        return [Entity.model_validate_json(r["payload"]) for r in rows]

    def add_alias(self, entity_id: str, alias: str) -> Entity:
        """Attach a ref string to an entity — 'Priya-in-Slack is
        Priya-in-the-standup'. Idempotent."""
        entity = self.get_entity(entity_id)
        if alias not in entity.aliases:
            entity.aliases.append(alias)
            self._conn.execute(
                "UPDATE entities SET payload = ? WHERE id = ?",
                (entity.model_dump_json(), entity_id),
            )
            self._record_change(
                "entities", "update", entity_id, entity.model_dump_json()
            )
            self._conn.commit()
        return entity

    def resolve_alias(self, ref: str) -> Optional[Entity]:
        """Which entity (if any) a ref string denotes: exact name/alias match
        first, then casefolded/stripped."""
        entities = self.list_entities()
        for entity in entities:
            if ref == entity.name or ref in entity.aliases:
                return entity
        needle = ref.casefold().strip()
        for entity in entities:
            if needle == entity.name.casefold().strip() or any(
                needle == a.casefold().strip() for a in entity.aliases
            ):
                return entity
        return None

    def units_by_entity(self, entity_id: str) -> list[Unit]:
        """Units that involve the entity: the union of its mentions/about
        edges (auto-linking) and a ref scan over payload roles + evidence
        speakers/authors (covers units linked only by hand-curated aliases)."""
        entity = self.get_entity(entity_id)
        by_id: dict[str, Unit] = {}
        for edge in self.edges_for(entity_id):
            other = edge.from_id if edge.to_id == entity_id else edge.to_id
            if other.startswith("unit_") and other not in by_id:
                by_id[other] = self.get_unit(other)
        refs = set(entity.aliases) | {entity.name}
        for unit in self.list_units():
            if unit.id not in by_id and unit_refs(unit) & refs:
                by_id[unit.id] = unit
        return sorted(by_id.values(), key=lambda u: u.ingested_at)

    # -- edges ---------------------------------------------------------------

    def add_edge(
        self,
        from_id: str,
        to_id: str,
        kind: str = "related",
        *,
        method: str = "manual",
        confidence: Optional[float] = None,
        rationale: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> Edge:
        """Link two nodes (units and/or entities). Duplicate (from, to, kind)
        triples return the already-stored edge unchanged."""
        self._require_node(from_id)
        self._require_node(to_id)
        edge = Edge(
            from_id=from_id,
            to_id=to_id,
            kind=kind,
            method=method,
            confidence=confidence,
            rationale=rationale,
            ref=ref,
        )
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO edges"
            " (id, from_id, to_id, kind, method, confidence, ref, rationale, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge.id,
                edge.from_id,
                edge.to_id,
                edge.kind,
                edge.method,
                edge.confidence,
                edge.ref,
                edge.rationale,
                _iso(edge.created_at),
            ),
        )
        if cur.rowcount > 0:
            # Dedupe drops are not writes — only real inserts replicate.
            self._record_change("edges", "insert", edge.id, edge.model_dump_json())
        self._conn.commit()
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT * FROM edges WHERE from_id = ? AND to_id = ? AND kind = ?",
                (from_id, to_id, kind),
            ).fetchone()
            return self._edge_from_row(row)
        return edge

    def _require_node(self, node_id: str) -> None:
        # Edges are polymorphic (no SQL FK possible) — existence is checked
        # here so auto-linking bugs can't silently accumulate dangling edges.
        if node_id.startswith("unit_"):
            self.get_unit(node_id)
        elif node_id.startswith("ent_"):
            self.get_entity(node_id)
        else:
            raise StoreError(f"not a linkable node id (unit_*/ent_*): {node_id}")

    def list_edges(self) -> list[Edge]:
        rows = self._conn.execute("SELECT * FROM edges ORDER BY created_at").fetchall()
        return [self._edge_from_row(r) for r in rows]

    def edges_for(self, node_id: str, kind: Optional[str] = None) -> list[Edge]:
        sql = "SELECT * FROM edges WHERE (from_id = ? OR to_id = ?)"
        args: list = [node_id, node_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = self._conn.execute(sql, args).fetchall()
        return [self._edge_from_row(r) for r in rows]

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> Edge:
        return Edge(
            id=row["id"],
            from_id=row["from_id"],
            to_id=row["to_id"],
            kind=row["kind"],
            method=row["method"],
            confidence=row["confidence"],
            ref=row["ref"],
            rationale=row["rationale"],
            created_at=row["created_at"],
        )

    # -- sync state (per-source pull watermarks) -----------------------------

    def get_sync_state(self, source: str) -> Optional[tuple[str, str]]:
        """(cursor, last_synced_at ISO) for a source, or None if never synced."""
        row = self._conn.execute(
            "SELECT cursor, last_synced_at FROM sync_state WHERE source = ?",
            (source,),
        ).fetchone()
        return None if row is None else (row["cursor"], row["last_synced_at"])

    def set_sync_state(self, source: str, cursor: str) -> None:
        self._conn.execute(
            "INSERT INTO sync_state (source, cursor, last_synced_at) VALUES (?, ?, ?)"
            " ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor,"
            " last_synced_at = excluded.last_synced_at",
            (source, cursor, _iso(datetime.now(timezone.utc))),
        )
        self._conn.commit()

    # -- workspace replication (M14) -----------------------------------------
    #
    # The outbox (`changelog`) is written by the normal write paths above;
    # `apply_change` is the trusted apply door for changes pulled from the
    # workspace log. "Trusted" is narrow: unit inserts still re-run the
    # evidence gate against the locally replicated episode, so the HARD
    # INVARIANT holds at this door too. Anything unapplicable is quarantined
    # (visible divergence), never silently dropped.

    def pending_changes(self, limit: int = 200) -> list[dict]:
        """Unpushed outbox records, oldest first, as ChangeRecord dicts."""
        rows = self._conn.execute(
            "SELECT change_id, table_name, op, row_id, payload, recorded_at"
            " FROM changelog WHERE pushed = 0 ORDER BY seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "change_id": r["change_id"],
                "table": r["table_name"],
                "op": r["op"],
                "row_id": r["row_id"],
                "payload": json.loads(r["payload"]),
                "recorded_at": r["recorded_at"],
            }
            for r in rows
        ]

    def record_change(self, table: str, op: str, row_id: str, payload_json: str) -> None:
        """Public outbox append — used by bootstrap seeding to enqueue rows
        that predate the changelog (pre-v4 data)."""
        self._record_change(table, op, row_id, payload_json)
        self._conn.commit()

    def changelog_covered(self) -> set[tuple[str, str]]:
        """(table, row_id) pairs that already have an insert change record —
        rows born after v4, which must not be double-seeded."""
        rows = self._conn.execute(
            "SELECT DISTINCT table_name, row_id FROM changelog WHERE op = 'insert'"
        ).fetchall()
        return {(r["table_name"], r["row_id"]) for r in rows}

    def mark_pushed(self, change_ids: list[str]) -> None:
        self._conn.executemany(
            "UPDATE changelog SET pushed = 1 WHERE change_id = ?",
            [(cid,) for cid in change_ids],
        )
        self._conn.commit()

    def get_replication_state(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM replication_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def set_replication_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO replication_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def has_pushed_changes(self) -> bool:
        """True iff this store has ever uploaded to SOME workspace — the
        signal that a workspace mismatch needs a human decision."""
        row = self._conn.execute(
            "SELECT 1 FROM changelog WHERE pushed = 1 LIMIT 1"
        ).fetchone()
        return row is not None

    def reset_replication_for_rebind(self) -> int:
        """Point this store's replication at a NEW (empty) workspace log:
        every outbox record becomes pending again (the full graph re-uploads
        in seq order), the pull cursor rewinds, and quarantine — stale
        against a log that no longer exists — is cleared. Local knowledge
        rows are untouched. Returns the number of re-queued changes."""
        cur = self._conn.execute("UPDATE changelog SET pushed = 0")
        requeued = cur.rowcount
        self._conn.execute(
            "INSERT INTO replication_state (key, value) VALUES ('pull_cursor', '0')"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        self._conn.execute("DELETE FROM replication_quarantine")
        self._conn.commit()
        return requeued

    def quarantine_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM replication_quarantine"
        ).fetchone()[0]

    def list_quarantine(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT change_id, envelope, reason, received_at"
            " FROM replication_quarantine ORDER BY received_at"
        ).fetchall()
        return [
            {
                "change_id": r["change_id"],
                "envelope": json.loads(r["envelope"]),
                "reason": r["reason"],
                "received_at": r["received_at"],
            }
            for r in rows
        ]

    def _quarantine(self, envelope: dict, reason: str) -> str:
        logger.warning(
            "REPLICATION-QUARANTINE %s: %s %s %s",
            reason,
            envelope.get("op"),
            envelope.get("table"),
            envelope.get("row_id"),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO replication_quarantine"
            " (change_id, envelope, reason, received_at) VALUES (?, ?, ?, ?)",
            (
                envelope["change_id"],
                json.dumps(envelope),
                reason,
                _iso(datetime.now(timezone.utc)),
            ),
        )
        self._conn.commit()
        return f"quarantined:{reason}"

    def retry_quarantine(self) -> int:
        """Re-apply every quarantined change (dependencies may have arrived
        since). Loops to a fixpoint so intra-quarantine dependencies (an edge
        waiting on a quarantined unit) clear in one call. Returns how many
        cleared. Runs before each pull apply."""
        cleared = 0
        progressed = True
        while progressed:
            progressed = False
            for item in self.list_quarantine():
                outcome = self.apply_change(item["envelope"])
                if not outcome.startswith("quarantined"):
                    self._conn.execute(
                        "DELETE FROM replication_quarantine WHERE change_id = ?",
                        (item["change_id"],),
                    )
                    self._conn.commit()
                    cleared += 1
                    progressed = True
        return cleared

    def apply_change(self, envelope: dict) -> str:
        """Apply one pulled change. Idempotent — re-applying is a no-op.
        Returns 'applied', 'skipped', or 'quarantined:<reason>'."""
        table, op = envelope["table"], envelope["op"]
        payload = envelope["payload"]
        row_id = envelope["row_id"]
        was_logging = self._log_changes
        self._log_changes = False
        try:
            if table == "episodes":
                return self._apply_episode(envelope, payload, row_id)
            if table == "units" and op == "insert":
                return self._apply_unit_insert(envelope, payload, row_id)
            if table == "units" and op == "update":
                return self._apply_unit_update(envelope, payload, row_id)
            if table == "entities" and op == "insert":
                return self._apply_entity_insert(payload, row_id)
            if table == "entities" and op == "update":
                return self._apply_entity_update(envelope, payload, row_id)
            if table == "edges":
                return self._apply_edge_insert(envelope, payload, row_id)
            return self._quarantine(envelope, "unknown-change-shape")
        finally:
            self._log_changes = was_logging

    def _row_exists(self, table: str, row_id: str) -> bool:
        return (
            self._conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)  # noqa: S608
            ).fetchone()
            is not None
        )

    def _apply_episode(self, envelope: dict, payload: dict, row_id: str) -> str:
        if envelope["op"] == "update":
            return self._apply_episode_update(envelope, payload, row_id)
        if self._row_exists("episodes", row_id):
            return "skipped"
        episode = Episode.model_validate(payload)
        if episode.external_id is not None:
            existing = self.get_episode_by_external_id(episode.external_id)
            if existing is not None and existing.id != episode.id:
                # Two devices ingested the same source item as different
                # episodes — the same-source-two-devices tripwire.
                return self._quarantine(envelope, "external-id-conflict")
        self.add_episode(episode)
        return "applied"

    def _apply_episode_update(self, envelope: dict, payload: dict, row_id: str) -> str:
        if not self._row_exists("episodes", row_id):
            return self._quarantine(envelope, "missing-row")
        remote = Episode.model_validate(payload)
        local = self.get_episode(row_id)
        if local.title == remote.title:
            return "skipped"
        # Title only — raw/external_id/timing are immutable history. Episodes
        # carry no update timestamp, so concurrent renames on two devices are
        # last-applied-wins (NOT commutative — devices may transiently diverge
        # until the next rename). Accepted v0, like duplicate same-name entities.
        self._conn.execute(
            "UPDATE episodes SET title = ? WHERE id = ?", (remote.title, row_id)
        )
        self._conn.commit()
        return "applied"

    def _apply_unit_insert(self, envelope: dict, payload: dict, row_id: str) -> str:
        if self._row_exists("units", row_id):
            return "skipped"
        unit = Unit.model_validate(payload)
        try:
            episode = self.get_episode(unit.provenance.episode_id)
        except StoreError:
            return self._quarantine(envelope, "missing-episode")
        try:
            # The HARD INVARIANT holds at the replication door too: evidence
            # is re-validated against the locally replicated raw content.
            validate_unit_evidence(unit, episode)
        except ExtractionGateError:
            logger.warning(
                "REPLICATION-GATE-FAIL unit %s from device %s failed the"
                " evidence gate against its episode",
                row_id,
                envelope.get("origin_device_id", "?"),
            )
            return self._quarantine(envelope, "gate-failed")
        self._insert_unit(unit)
        self._conn.commit()
        return "applied"

    def _apply_unit_update(self, envelope: dict, payload: dict, row_id: str) -> str:
        if not self._row_exists("units", row_id):
            return self._quarantine(envelope, "missing-row")
        remote = Unit.model_validate(payload)
        local = self.get_unit(row_id)
        if remote.valid_to is None:
            return "skipped"  # malformed update — nothing to close
        if local.valid_to is not None and local.valid_to <= remote.valid_to:
            # Concurrent supersede on two devices: keep min(valid_to) — it's
            # commutative, so every device converges on the earliest close.
            return "skipped"
        self._conn.execute(
            "UPDATE units SET valid_to = ?, payload = ? WHERE id = ?",
            (_iso(remote.valid_to), remote.model_dump_json(), row_id),
        )
        self._conn.commit()
        return "applied"

    def _apply_entity_insert(self, payload: dict, row_id: str) -> str:
        if self._row_exists("entities", row_id):
            return "skipped"
        self.add_entity(Entity.model_validate(payload))
        return "applied"

    def _apply_entity_update(self, envelope: dict, payload: dict, row_id: str) -> str:
        if not self._row_exists("entities", row_id):
            return self._quarantine(envelope, "missing-row")
        remote = Entity.model_validate(payload)
        local = self.get_entity(row_id)
        # Alias set-union merge (commutative): local order, then new remote.
        new_aliases = [a for a in remote.aliases if a not in local.aliases]
        if not new_aliases:
            return "skipped"
        local.aliases.extend(new_aliases)
        self._conn.execute(
            "UPDATE entities SET payload = ? WHERE id = ?",
            (local.model_dump_json(), row_id),
        )
        self._conn.commit()
        return "applied"

    def _apply_edge_insert(self, envelope: dict, payload: dict, row_id: str) -> str:
        edge = Edge.model_validate(payload)
        try:
            self._require_node(edge.from_id)
            self._require_node(edge.to_id)
        except StoreError:
            return self._quarantine(envelope, "missing-node")
        # Direct insert preserving the origin's id/created_at; the store's
        # (from, to, kind) UNIQUE still dedupes.
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO edges"
            " (id, from_id, to_id, kind, method, confidence, ref, rationale, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge.id,
                edge.from_id,
                edge.to_id,
                edge.kind,
                edge.method,
                edge.confidence,
                edge.ref,
                edge.rationale,
                _iso(edge.created_at),
            ),
        )
        self._conn.commit()
        return "applied" if cur.rowcount > 0 else "skipped"
