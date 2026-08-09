"""Automatic linking — the Graphiti-style pipeline, with the quote-gate
philosophy extended to edges: every machine-made edge carries method +
confidence + rationale, so trust stays inspectable.

Runs AFTER units are stored. It NEVER fails the ingest: every step is fenced;
a failure logs and leaves the units stored, unlinked (relinkable later).

Three steps:
1. embed new units (search indexing).
2. entity mentions — resolve PersonRef strings against entities (tiers:
   exact -> fuzzy -> auto-create); bare SPEAKER_NN diarization labels are
   NEVER resolved (episode-scoped artifacts — they wait for hand-linked
   aliases). API mode adds one LLM pass for non-person entities.
3. relatedness — hybrid-retrieve candidate prior units per new unit; API mode
   classifies each pair (related/duplicate/contradicts/supersedes) and may
   AUTO-SUPERSEDE under strict guards; local mode caps at cosine 'related'
   edges and never supersedes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from vetromar.config import Config
from vetromar.linking.prompts import (
    MENTION_SYSTEM_PROMPT,
    MentionResult,
    PAIR_SYSTEM_PROMPT,
    PairVerdicts,
    build_mention_prompt,
    build_pair_prompt,
)
from vetromar.schema import Entity, Unit
from vetromar.search import hybrid
from vetromar.store import Store, StoreError
from vetromar.store.store import unit_refs

log = logging.getLogger("vetromar.linking")

SPEAKER_LABEL = re.compile(r"SPEAKER_\d+")
RELATED_COSINE_THRESHOLD = 0.80   # passage<->passage space (local tier)
SUPERSEDE_CONFIDENCE = 0.80       # LLM confidence floor for auto-supersede
CANDIDATES_PER_UNIT = 5
MAX_PAIRS_PER_CALL = 24           # pair-verdict prompt size cap
INTRA_BATCH_WINDOW = 8            # earlier same-batch units judged per unit


@dataclass
class LinkReport:
    mentions: int = 0
    entities_created: int = 0
    relations: int = 0
    superseded: int = 0
    errors: list[str] = field(default_factory=list)


def auto_link(store: Store, units: list[Unit], config: Config | None = None) -> LinkReport:
    """Link freshly-stored units into the graph. Never raises."""
    report = LinkReport()
    if not units:
        return report
    _fenced(report, "index", lambda: hybrid.index_units(store, units))
    _fenced(report, "mentions", lambda: _resolve_person_mentions(store, units, report))
    from vetromar.ai import ai_available

    if config is not None and config.backend == "api" and ai_available(config):
        _fenced(report, "llm-mentions", lambda: _resolve_llm_mentions(store, units, config, report))
        _fenced(report, "llm-relations", lambda: _link_related_llm(store, units, config, report))
    else:
        _fenced(report, "embed-relations", lambda: _link_related_embed(store, units, report))
    return report


def _fenced(report: LinkReport, step: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — linking must never fail the ingest
        log.exception("auto-link step %r failed", step)
        report.errors.append(f"{step}: {exc}")


# -- step 2: person mentions --------------------------------------------------


def _resolve_person_mentions(store: Store, units: list[Unit], report: LinkReport) -> None:
    for unit in units:
        for ref in sorted(unit_refs(unit)):
            if SPEAKER_LABEL.fullmatch(ref):
                continue  # diarization labels are episode-scoped, never global identities
            entity = store.resolve_alias(ref)
            if entity is not None:
                exact = ref == entity.name or ref in entity.aliases
                store.add_edge(
                    unit.id, entity.id, kind="mentions",
                    method="auto-exact" if exact else "auto-fuzzy",
                    confidence=1.0 if exact else 0.8,
                    ref=ref,
                )
            else:
                entity = store.add_entity(Entity(name=ref, type="person", aliases=[ref]))
                store.add_edge(
                    unit.id, entity.id, kind="mentions",
                    method="auto-create", confidence=0.9, ref=ref,
                    rationale="entity auto-created from unresolved named ref",
                )
                report.entities_created += 1
            report.mentions += 1


# -- API-mode LLM steps -------------------------------------------------------


def _parse_call(config: Config, system: str, user: str, output_format):
    from vetromar.ai import get_provider

    return get_provider(config).parse_structured(
        system=system, user=user, schema=output_format, max_tokens=8192
    )


def _llm_mentions(config: Config, unit_texts: list[str]) -> MentionResult:
    return _parse_call(config, MENTION_SYSTEM_PROMPT, build_mention_prompt(unit_texts), MentionResult)


def _llm_pairs(config: Config, pairs: list[tuple[str, str]]) -> PairVerdicts:
    return _parse_call(config, PAIR_SYSTEM_PROMPT, build_pair_prompt(pairs), PairVerdicts)


def _unit_text(unit: Unit) -> str:
    text = unit.content
    if unit.reasoning:
        text += f" — {unit.reasoning}"
    return text


def _resolve_llm_mentions(store: Store, units: list[Unit], config: Config, report: LinkReport) -> None:
    result = _llm_mentions(config, [_unit_text(u) for u in units])
    for mention in result.mentions:
        if not (0 <= mention.unit_index < len(units)):
            continue
        unit = units[mention.unit_index]
        entity = store.resolve_alias(mention.name) or store.resolve_alias(mention.ref)
        if entity is None:
            entity = store.add_entity(
                Entity(name=mention.name, type=mention.type, aliases=[mention.ref])
            )
            report.entities_created += 1
        store.add_edge(
            unit.id, entity.id, kind="about",
            method="auto-llm", confidence=mention.confidence, ref=mention.ref,
        )
        report.mentions += 1


def _candidates(store: Store, unit: Unit, exclude: set[str]) -> list[Unit]:
    hits = hybrid.search(store, unit.content, k=CANDIDATES_PER_UNIT * 2, current_only=True)
    return [h.unit for h in hits if h.unit.id not in exclude][:CANDIDATES_PER_UNIT]


def _link_related_llm(store: Store, units: list[Unit], config: Config, report: LinkReport) -> None:
    new_ids = {u.id for u in units}
    pairs: list[tuple[Unit, Unit]] = []
    for i, unit in enumerate(units):
        for candidate in _candidates(store, unit, exclude=new_ids):
            pairs.append((unit, candidate))
        # Earlier units of the SAME batch are candidates too: an in-meeting
        # reversal (a recalled prior decision + the decision reversing it,
        # extracted together) must be judged. Later-vs-earlier keeps the
        # NEW/EXISTING direction aligned with source order. Windowed so a big
        # ingest stays O(batch), not O(batch^2) — a reversal and the decision
        # it reverses sit near each other in source order.
        for earlier in units[max(0, i - INTRA_BATCH_WINDOW):i]:
            pairs.append((unit, earlier))
    # Chunked prompts: one huge pair list in a single call degrades cheap
    # models and hits output limits; verdict indices are per-chunk.
    for start in range(0, len(pairs), MAX_PAIRS_PER_CALL):
        chunk = pairs[start : start + MAX_PAIRS_PER_CALL]
        verdicts = _llm_pairs(config, [(_unit_text(n), _unit_text(e)) for n, e in chunk])
        for verdict in verdicts.verdicts:
            if not (0 <= verdict.pair_index < len(chunk)) or verdict.relation == "none":
                continue
            new, existing = chunk[verdict.pair_index]
            store.add_edge(
                new.id, existing.id, kind=verdict.relation,
                method="auto-llm", confidence=verdict.confidence, rationale=verdict.rationale,
            )
            report.relations += 1
            if verdict.relation == "supersedes":
                _maybe_auto_supersede(store, new, existing, verdict.confidence, report)


def _maybe_auto_supersede(
    store: Store, new: Unit, existing: Unit, confidence: float, report: LinkReport
) -> None:
    # Guards: closing validity is consequential and has no undo — only on a
    # confident, same-kind verdict against a still-current unit.
    if confidence < SUPERSEDE_CONFIDENCE:
        log.info("supersedes edge below threshold (%.2f) — edge only, no auto-close", confidence)
        return
    if new.type != existing.type:
        log.info("cross-kind supersedes (%s over %s) — edge only", new.type, existing.type)
        return
    try:
        store.supersede(existing.id, new.id)
        report.superseded += 1
        log.warning(
            "AUTO-SUPERSEDED %s with %s (confidence %.2f)", existing.id, new.id, confidence
        )
    except StoreError as exc:
        log.info("auto-supersede skipped: %s", exc)


# -- local-mode relatedness (no LLM) ------------------------------------------


def _link_related_embed(store: Store, units: list[Unit], report: LinkReport) -> None:
    """Passage<->passage cosine over stored embeddings. Related edges only —
    similarity alone is never grounds to close a unit's validity."""
    import numpy as np

    new_ids = {u.id for u in units}
    for i, unit in enumerate(units):
        blob = store.get_embedding(unit.id)
        if blob is None:
            continue  # embedder unavailable — nothing to score with
        vec = np.frombuffer(blob, dtype=np.float32)
        vec = vec / max(float(np.linalg.norm(vec)), 1e-9)
        for candidate in _candidates(store, unit, exclude=new_ids) + list(
            units[max(0, i - INTRA_BATCH_WINDOW):i]
        ):
            cand_blob = store.get_embedding(candidate.id)
            if cand_blob is None:
                continue
            cand = np.frombuffer(cand_blob, dtype=np.float32)
            cand = cand / max(float(np.linalg.norm(cand)), 1e-9)
            similarity = float(vec @ cand)
            if similarity >= RELATED_COSINE_THRESHOLD:
                store.add_edge(
                    unit.id, candidate.id, kind="related",
                    method="auto-embed", confidence=round(similarity, 3),
                    rationale=f"passage cosine {similarity:.2f}",
                )
                report.relations += 1
