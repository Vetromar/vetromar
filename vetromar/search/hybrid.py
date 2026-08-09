"""Hybrid retrieval: FTS5 (BM25) ∪ embedding cosine → reciprocal-rank fusion.

FTS carries exact-term recall, vectors carry paraphrase recall; RRF fuses the
two rank lists without any score calibration. Degrades to FTS-only when the
embedder is unavailable (offline first run) or no vectors exist yet — search
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
    backstop."""
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

    # Reciprocal-rank fusion over the two rank lists.
    channel_ranks: dict[str, list[int]] = {}
    for rank, (unit, _score) in enumerate(fts_hits):
        channel_ranks.setdefault(unit.id, []).append(rank)
    for rank, (unit_id, _dist) in enumerate(vec_hits):
        channel_ranks.setdefault(unit_id, []).append(rank)
    fused = {
        unit_id: sum(1.0 / (RRF_K + rank + 1) for rank in ranks)
        for unit_id, ranks in channel_ranks.items()
    }

    by_id = {unit.id: unit for unit, _ in fts_hits}
    vec_only = [uid for uid in fused if uid not in by_id]
    by_id.update({unit.id: unit for unit in store.get_units(vec_only)})
    results: list[ScoredUnit] = []
    for unit_id, score in sorted(fused.items(), key=lambda kv: -kv[1]):
        unit = by_id.get(unit_id)
        if unit is None:
            continue  # vector index trailing the truth tables
        if matches_filters(unit, **filters):
            results.append(ScoredUnit(unit=unit, score=score))
    return results[:k]
