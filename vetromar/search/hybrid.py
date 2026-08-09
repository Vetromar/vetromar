"""Hybrid retrieval: four channels → reciprocal-rank fusion (→ optional
cross-encoder rerank).

- FTS5 (BM25) carries exact-term recall; embedding cosine carries paraphrase
  recall (both filter-pushed into SQL).
- The ENTITY channel resolves the query against entity names/aliases (exact +
  vector) and surfaces the units linked to those entities — "what do we know
  about Priya" works even when no unit text contains "Priya".
- The GRAPH channel expands the top fused seeds one hop along edges — the
  Polygres move: relationships are a retrieval signal, not just decoration.

RRF fuses the rank lists without score calibration. Every channel degrades
independently (no embedder → FTS-only; no entities → two channels) — search
never fails because indexing did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from vetromar.schema import Unit
from vetromar.search import embedder
from vetromar.search.embedder import EmbedderUnavailableError
from vetromar.store import Store
from vetromar.store.store import fts_text

log = logging.getLogger("vetromar.search")

RRF_K = 60          # standard reciprocal-rank-fusion constant
CHANNEL_DEPTH = 50  # candidates pulled from each channel before fusion
ENTITY_SIM_THRESHOLD = 0.75  # query<->entity cosine floor for the entity channel
GRAPH_SEEDS = 5     # top fused hits expanded by the graph channel
RERANK_POOL = 30    # fused candidates re-scored by the optional cross-encoder


@dataclass
class ScoredUnit:
    unit: Unit
    score: float


def index_units(store: Store, units: list[Unit]) -> int:
    """Embed + store vectors for units. NEVER raises — capture/ingest must not
    fail because search indexing hiccuped; anything missed is backfilled on
    the next search. Returns how many units got vectors."""
    if not units:
        return 0
    try:
        blobs = embedder.embed_passages([fts_text(u) for u in units])
        for unit, blob in zip(units, blobs):
            store.put_embedding(unit.id, blob)
        return len(units)
    except EmbedderUnavailableError as exc:
        log.warning("embedding skipped (%s) — search will be FTS-only until it loads", exc)
        return 0
    except Exception:  # noqa: BLE001
        log.exception("embedding failed — search will backfill later")
        return 0


def ensure_indexed(store: Store) -> None:
    """Lazy backfill: any unit still awaiting a vector gets one now (covers
    units ingested while the embedder was unavailable, and migrated stores).
    O(1) when nothing is pending — an EXISTS probe on the dirty set, not a
    scan of every unit — so it's safe on every search."""
    if not store.has_pending_embeddings():
        return
    while True:
        batch = store.units_pending_embedding(limit=256)
        if not batch:
            return
        if index_units(store, batch) == 0:
            return  # embedder unavailable — the dirty set keeps them queued


def matches_filters(
    unit: Unit,
    *,
    type: Optional[str] = None,
    status: Optional[str] = None,
    episode_id: Optional[str] = None,
    method: Optional[str] = None,
    current_only: bool = False,
    as_of: Optional[datetime] = None,
) -> bool:
    if type and unit.type != type:
        return False
    if status:
        unit_status = getattr(unit.payload, "status", None)
        if unit_status is None or unit_status.value != status:
            return False
    if episode_id and unit.provenance.episode_id != episode_id:
        return False
    if method and unit.provenance.method != method:
        return False
    if current_only and unit.valid_to is not None:
        return False
    if as_of is not None:
        if unit.valid_from > as_of or (unit.valid_to is not None and unit.valid_to <= as_of):
            return False
    return True


def search(
    store: Store,
    query: str,
    k: int = 10,
    *,
    type: Optional[str] = None,
    status: Optional[str] = None,
    episode_id: Optional[str] = None,
    method: Optional[str] = None,
    current_only: bool = False,
    as_of: Optional[datetime] = None,
) -> list[ScoredUnit]:
    """Ranked hybrid search. Filters are pushed into each channel (SQL for
    FTS, bulk id-filtering with iterative deepening for vectors) so channel
    depth means matching candidates; `matches_filters` stays as a post-fusion
    backstop. With `rerank_enabled` config a local cross-encoder re-scores
    the fused pool (degrades silently to the fused order)."""
    filters = dict(
        type=type,
        status=status,
        episode_id=episode_id,
        method=method,
        current_only=current_only,
        as_of=as_of,
    )
    fts_hits = store.search_fts(query, k=CHANNEL_DEPTH, **filters)

    vec_hits: list[tuple[str, float]] = []
    query_vec: Optional[bytes] = None
    try:
        ensure_indexed(store)
        query_vec = embedder.embed_query(query)
        # KNN can't pre-filter: fetch, drop filtered-out ids in one SQL pass,
        # and if the surviving pool is short (most neighbors superseded, say)
        # retry once at 8x depth — a two-step ladder, because brute-force KNN
        # cost barely depends on k, so intermediate rungs only add scans.
        for n in (CHANNEL_DEPTH, CHANNEL_DEPTH * 8):
            raw_hits = store.vector_topk(query_vec, n=n)
            allowed = store.filter_unit_ids([uid for uid, _ in raw_hits], **filters)
            vec_hits = [(uid, dist) for uid, dist in raw_hits if uid in allowed]
            if len(vec_hits) >= CHANNEL_DEPTH or len(raw_hits) < n:
                break
    except EmbedderUnavailableError:
        pass  # FTS-only degrade, already logged at index time

    entity_ids = _entity_channel_ids(store, query, query_vec)
    entity_hits = _units_for_entities(store, entity_ids, filters)

    # Fuse the three query channels, then let the graph channel expand the
    # top seeds one hop — related units earn a rank list of their own.
    channel_ranks: dict[str, list[int]] = {}

    def _add_channel(unit_ids: list[str]) -> None:
        for rank, unit_id in enumerate(unit_ids):
            channel_ranks.setdefault(unit_id, []).append(rank)

    _add_channel([unit.id for unit, _ in fts_hits])
    _add_channel([uid for uid, _ in vec_hits])
    _add_channel(entity_hits)

    def _fuse() -> dict[str, float]:
        return {
            unit_id: sum(1.0 / (RRF_K + rank + 1) for rank in ranks)
            for unit_id, ranks in channel_ranks.items()
        }

    fused = _fuse()
    seeds = [uid for uid, _ in sorted(fused.items(), key=lambda kv: -kv[1])[:GRAPH_SEEDS]]
    graph_hits = _graph_channel_ids(store, seeds, set(fused), filters)
    if graph_hits:
        _add_channel(graph_hits)
        fused = _fuse()

    by_id = {unit.id: unit for unit, _ in fts_hits}
    missing = [uid for uid in fused if uid not in by_id]
    by_id.update({unit.id: unit for unit in store.get_units(missing)})
    results: list[ScoredUnit] = []
    for unit_id, score in sorted(fused.items(), key=lambda kv: -kv[1]):
        unit = by_id.get(unit_id)
        if unit is None:
            continue  # vector index trailing the truth tables
        if matches_filters(unit, **filters):
            results.append(ScoredUnit(unit=unit, score=score))
    return _maybe_rerank(query, results, k)


def _entity_channel_ids(
    store: Store, query: str, query_vec: Optional[bytes]
) -> list[str]:
    """Entities the query plausibly denotes: exact/casefold alias match on
    the whole query first, then entity-vector neighbors above the cosine
    floor. Ordered best-first; empty for ordinary non-entity queries."""
    entity_ids: list[str] = []
    exact = store.resolve_alias(query.strip())
    if exact is not None:
        entity_ids.append(exact.id)
    if query_vec is not None:
        try:
            for entity_id, dist in store.entity_vector_topk(query_vec, n=5):
                if 1.0 - dist < ENTITY_SIM_THRESHOLD:
                    continue
                canonical = store.resolve_entity(entity_id)
                if canonical.id not in entity_ids:
                    entity_ids.append(canonical.id)
        except Exception:  # noqa: BLE001 — a channel never fails the search
            log.exception("entity channel failed — continuing without it")
    return entity_ids


def _units_for_entities(store: Store, entity_ids: list[str], filters: dict) -> list[str]:
    """The units linked to the matched entities (mentions/about edges),
    newest first per entity, filter-checked in bulk."""
    unit_ids: list[str] = []
    for entity_id in entity_ids:
        linked: list[str] = []
        for edge in store.edges_for(entity_id):
            if edge.kind not in ("mentions", "about"):
                continue
            other = edge.from_id if edge.to_id == entity_id else edge.to_id
            if other.startswith("unit_") and other not in linked:
                linked.append(other)
        allowed = store.filter_unit_ids(linked, **filters)
        ordered = sorted(allowed)  # stable before recency sort below
        units = store.get_units(ordered)
        units.sort(key=lambda u: u.ingested_at, reverse=True)
        for unit in units[:CHANNEL_DEPTH]:
            if unit.id not in unit_ids:
                unit_ids.append(unit.id)
    return unit_ids


def _graph_channel_ids(
    store: Store, seeds: list[str], already: set[str], filters: dict
) -> list[str]:
    """One-hop edge expansion of the top fused seeds — units related to what
    the query channels found, in seed order."""
    expanded: list[str] = []
    try:
        for seed in seeds:
            for edge in store.edges_for(seed, current_only=True):
                other = edge.from_id if edge.to_id == seed else edge.to_id
                if (
                    other.startswith("unit_")
                    and other not in already
                    and other not in expanded
                ):
                    expanded.append(other)
        allowed = store.filter_unit_ids(expanded, **filters)
        return [uid for uid in expanded if uid in allowed]
    except Exception:  # noqa: BLE001 — a channel never fails the search
        log.exception("graph channel failed — continuing without it")
        return []


def _maybe_rerank(query: str, results: list[ScoredUnit], k: int) -> list[ScoredUnit]:
    """Cross-encoder re-scoring of the fused pool when enabled; any failure
    (flag off, model unavailable) returns the fused order."""
    from vetromar.config import load_config

    try:
        if len(results) <= 1 or not load_config().rerank_enabled:
            return results[:k]
        from vetromar.search import reranker

        pool = results[:RERANK_POOL]
        scores = reranker.rerank(query, [r.unit.content for r in pool])
        reranked = [
            ScoredUnit(unit=r.unit, score=s)
            for r, s in sorted(zip(pool, scores), key=lambda pair: -pair[1])
        ]
        return (reranked + results[RERANK_POOL:])[:k]
    except Exception:  # noqa: BLE001 — reranking never fails the search
        log.warning("rerank unavailable — returning fused order")
        return results[:k]
