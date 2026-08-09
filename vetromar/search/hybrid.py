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
    """Lazy backfill: any unit without a vector gets one now (covers units
    ingested while the embedder was unavailable, and pre-B stores)."""
    missing = store.units_missing_embedding()
    if missing:
        index_units(store, missing)


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
    """Ranked hybrid search with post-fusion filters."""
    fts_hits = store.search_fts(query, k=CHANNEL_DEPTH)

    vec_hits: list[tuple[str, float]] = []
    try:
        ensure_indexed(store)
        query_vec = embedder.embed_query(query)
        vec_hits = store.vector_topk(query_vec, n=CHANNEL_DEPTH)
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
    results: list[ScoredUnit] = []
    for unit_id, score in sorted(fused.items(), key=lambda kv: -kv[1]):
        unit = by_id.get(unit_id)
        if unit is None:
            unit = store.get_unit(unit_id)
        if matches_filters(
            unit,
            type=type,
            status=status,
            episode_id=episode_id,
            method=method,
            current_only=current_only,
            as_of=as_of,
        ):
            results.append(ScoredUnit(unit=unit, score=score))
    return results[:k]
