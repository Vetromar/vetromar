"""Entity deduplication — redirect-based merge, never edge rewriting.

Auto-created entities accumulate duplicates ("Priya", "priya.k", "Priya K"
from three sources). This pass finds candidate duplicate pairs and merges
them: the older entity stays canonical, the newer one's aliases are unioned
into it, and the newer one gets `merged_into` set. Reads follow the redirect
(`Store.resolve_entity`); edges are left untouched, so a merge is two
commutative entity updates on the replication wire — no new change shapes,
no repoint races between devices.

Tiers (the quote-gate philosophy applied to identity):
- exact tier — entities of the same type sharing a normalized name/alias are
  merged without asking a model. Deterministic, runs in every mode.
- embedding+LLM tier (API mode only) — entity-vector neighbors above
  EMBED_CANDIDATE_THRESHOLD are confirmed by a batched LLM pass and merged
  at MERGE_CONFIDENCE. Local mode never guesses beyond the exact tier
  (cheap-model constraint: a wrong merge poisons the graph).

Triggered fenced at the end of auto_link when entities were created, and on
demand (CLI `vetromar dedupe-entities`, POST /api/store/dedupe) for
retroactive cleanup before/after big ingests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, Field

from vetromar.config import Config
from vetromar.schema import Entity
from vetromar.store import Store
from vetromar.store.store import _norm_ref

log = logging.getLogger("vetromar.linking")

EMBED_CANDIDATE_THRESHOLD = 0.85  # entity-vector cosine floor for LLM pairs
MERGE_CONFIDENCE = 0.80           # LLM confidence floor to actually merge
MAX_PAIRS_PER_CALL = 24


@dataclass
class DedupeReport:
    merged: int = 0
    llm_pairs_judged: int = 0
    errors: list[str] = field(default_factory=list)


DEDUPE_SYSTEM_PROMPT = """\
You judge whether two entity records from a company knowledge store denote \
the SAME real-world thing (the same person, project, product, team, ...).

Records show name, type, aliases (strings seen referring to it), and an \
optional summary. Typical duplicates: name variants ("Priya" / "Priya K"), \
handles vs names ("priya.k" / "Priya Kumar"), abbreviations.

Be CONSERVATIVE: merging is consequential — different people sharing a first \
name are NOT the same. When in doubt, say different. confidence in [0,1].
"""


class EntityPairVerdict(BaseModel):
    pair_index: int = Field(description="0-based index of the judged pair")
    verdict: Literal["same", "different"]
    confidence: float = Field(description="Confidence in [0,1]")


class EntityPairVerdicts(BaseModel):
    verdicts: list[EntityPairVerdict]


def _entity_card(entity: Entity) -> str:
    parts = [f"name={entity.name!r}", f"type={entity.type}"]
    if entity.aliases:
        parts.append(f"aliases={entity.aliases!r}")
    if entity.summary:
        parts.append(f"summary={entity.summary!r}")
    return ", ".join(parts)


def _build_dedupe_prompt(pairs: list[tuple[Entity, Entity]]) -> str:
    blocks = [
        f"pair {i}:\n  A: {_entity_card(a)}\n  B: {_entity_card(b)}"
        for i, (a, b) in enumerate(pairs)
    ]
    body = "\n\n".join(blocks)
    return f"<pairs>\n{body}\n</pairs>\n\nJudge each pair: same entity or different?"


def entity_text(entity: Entity) -> str:
    """The embedded text for an entity — name + aliases + summary, mirroring
    fts_text's one-derivation rule."""
    parts = [entity.name, *entity.aliases]
    if entity.summary:
        parts.append(entity.summary)
    return "\n".join(parts)


def ensure_entities_indexed(store: Store) -> None:
    """Embed entities that don't have vectors yet. NEVER raises (embedder may
    be unavailable — the exact tier still works without vectors)."""
    from vetromar.search import embedder

    missing = store.entities_missing_embedding()
    if not missing:
        return
    try:
        blobs = embedder.embed_passages([entity_text(e) for e in missing])
        for entity, blob in zip(missing, blobs):
            store.put_entity_embedding(entity.id, blob)
    except Exception:  # noqa: BLE001 — indexing must never fail the caller
        log.warning("entity embedding unavailable — dedupe runs exact tier only")


def _canonical_order(a: Entity, b: Entity) -> tuple[Entity, Entity]:
    """(canonical, victim): older entity wins; id as deterministic tiebreak."""
    if (a.created_at, a.id) <= (b.created_at, b.id):
        return a, b
    return b, a


def merge_entities(store: Store, canonical_id: str, victim_id: str) -> None:
    """Union the victim's identity into the canonical entity and redirect it.
    Two commutative wire ops (alias-union update + merged_into update);
    edges stay where they are — reads resolve the redirect."""
    canonical = store.get_entity(canonical_id)
    victim = store.get_entity(victim_id)
    for alias in {victim.name, *victim.aliases}:
        if alias != canonical.name and alias not in canonical.aliases:
            store.add_alias(canonical_id, alias)
    if victim.summary and not canonical.summary:
        store.update_entity_profile(canonical_id, summary=victim.summary)
    new_attrs = {
        k: v for k, v in victim.attributes.items() if k not in canonical.attributes
    }
    if new_attrs:
        store.update_entity_profile(canonical_id, attributes=new_attrs)
    store.update_entity_profile(victim_id, merged_into=canonical_id)
    log.warning("ENTITY-MERGED %s (%r) into %s (%r)",
                victim_id, victim.name, canonical_id, canonical.name)


def _exact_tier(store: Store, entities: list[Entity], report: DedupeReport) -> set[str]:
    """Merge same-type entities sharing a normalized name/alias. Returns ids
    consumed (merged away or already handled)."""
    consumed: set[str] = set()
    for entity in entities:
        if entity.id in consumed or entity.merged_into:
            continue
        for alias in {entity.name, *entity.aliases}:
            rows = store._conn.execute(
                "SELECT DISTINCT entity_id FROM entity_aliases WHERE alias_norm = ?",
                (_norm_ref(alias),),
            ).fetchall()
            for row in rows:
                other_id = row["entity_id"]
                if other_id == entity.id or other_id in consumed:
                    continue
                other = store.resolve_entity(other_id)
                current = store.resolve_entity(entity.id)
                if other.id == current.id or other.type != current.type:
                    continue
                canonical, victim = _canonical_order(current, other)
                merge_entities(store, canonical.id, victim.id)
                consumed.add(victim.id)
                report.merged += 1
    return consumed


def _embedding_pairs(
    store: Store, entities: Iterable[Entity], consumed: set[str]
) -> list[tuple[Entity, Entity]]:
    from vetromar.search import embedder

    pairs: list[tuple[Entity, Entity]] = []
    seen: set[tuple[str, str]] = set()
    for entity in entities:
        if entity.id in consumed or entity.merged_into:
            continue
        blob = store._conn.execute(
            "SELECT embedding FROM entity_vectors WHERE entity_id = ?", (entity.id,)
        ).fetchone()
        if blob is None:
            continue
        for other_id, dist in store.entity_vector_topk(blob["embedding"], n=6):
            if other_id == entity.id or other_id in consumed:
                continue
            if 1.0 - dist < EMBED_CANDIDATE_THRESHOLD:
                continue
            other = store.resolve_entity(other_id)
            if other.id == entity.id or other.type != entity.type:
                continue
            key = tuple(sorted((entity.id, other.id)))
            if key not in seen:
                seen.add(key)
                pairs.append((entity, other))
    return pairs


def _llm_tier(
    store: Store,
    config: Config,
    pairs: list[tuple[Entity, Entity]],
    report: DedupeReport,
) -> None:
    from vetromar.ai import get_provider

    provider = get_provider(config)
    for start in range(0, len(pairs), MAX_PAIRS_PER_CALL):
        chunk = pairs[start : start + MAX_PAIRS_PER_CALL]
        result = provider.parse_structured(
            system=DEDUPE_SYSTEM_PROMPT,
            user=_build_dedupe_prompt(chunk),
            schema=EntityPairVerdicts,
            max_tokens=8192,
        )
        report.llm_pairs_judged += len(chunk)
        for verdict in result.verdicts:
            if not (0 <= verdict.pair_index < len(chunk)):
                continue
            if verdict.verdict != "same" or verdict.confidence < MERGE_CONFIDENCE:
                continue
            a, b = chunk[verdict.pair_index]
            a, b = store.resolve_entity(a.id), store.resolve_entity(b.id)
            if a.id == b.id:
                continue  # already merged via an earlier verdict
            canonical, victim = _canonical_order(a, b)
            merge_entities(store, canonical.id, victim.id)
            report.merged += 1


def dedupe_entities(
    store: Store,
    config: Optional[Config] = None,
    seeds: Optional[list[Entity]] = None,
) -> DedupeReport:
    """Run the dedup pass. `seeds` limits candidate discovery to those
    entities (the just-created ones after auto_link); None sweeps the whole
    store (the retro cleanup job). Never raises."""
    report = DedupeReport()
    try:
        entities = seeds if seeds is not None else store.list_entities()
        consumed = _exact_tier(store, entities, report)
        from vetromar.ai import ai_available

        if config is not None and config.backend == "api" and ai_available(config):
            ensure_entities_indexed(store)
            pairs = _embedding_pairs(store, entities, consumed)
            if pairs:
                _llm_tier(store, config, pairs, report)
    except Exception as exc:  # noqa: BLE001 — dedupe must never fail the caller
        log.exception("entity dedupe failed")
        report.errors.append(str(exc))
    return report
