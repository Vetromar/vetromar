"""Generic extraction: raw text of ANY source kind -> typed UnitDrafts.

API backend only in v1 — the local model's grammar path took eleven tuning
rounds for the meeting schema and hasn't been tuned for this one; a clean
ConfigError beats silently bad extraction. The meeting pipeline is unaffected
(it has its own frozen extractor).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from pydantic import BaseModel, Field

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction.chunking import SINGLE_CALL_LIMIT, chunk_text
from vetromar.extraction.repair import heal_draft_evidence
from vetromar.extraction.validate import _normalize, derive_haystack
from vetromar.extraction.generic_prompt import (
    GENERIC_SYSTEM_PROMPT,
    build_generic_user_prompt,
)
from vetromar.schema import Episode, ExcerptEvidence, UnitDraft, UnitPayload

logger = logging.getLogger("vetromar.extraction.generic")


class GenericDraft(BaseModel):
    """Model-facing draft: like UnitDraft but evidence is excerpt-only — text
    sources have no timestamps to quote and no datapoints to cite, so the
    model is never asked to produce them."""

    content: str = Field(description="One crisp sentence stating the claim/idea")
    reasoning: Optional[str] = Field(
        default=None, description="WHY, as given in the source text (omit if not given)"
    )
    payload: UnitPayload
    evidence: list[ExcerptEvidence] = Field(
        description="Verbatim excerpts from the source evidencing this unit (>=1)"
    )


class GenericExtractionResult(BaseModel):
    units: list[GenericDraft]


def _call_model(
    config: Config,
    episode: Episode,
    text: str,
    part: Optional[tuple[int, int]] = None,
) -> GenericExtractionResult:
    from vetromar.ai import get_provider, map_ai_error

    provider = get_provider(config)
    try:
        return provider.parse_structured(
            system=GENERIC_SYSTEM_PROMPT,
            user=build_generic_user_prompt(
                episode.source_kind, episode.title, text, part=part
            ),
            schema=GenericExtractionResult,
            max_tokens=16000,
        )
    except Exception as exc:
        mapped = map_ai_error(exc, config)
        if mapped is not None:
            raise mapped from exc
        raise


def _dedup_key(text: str) -> str:
    return _normalize(text).casefold()


def _is_duplicate(draft: UnitDraft, kept: UnitDraft) -> bool:
    if draft.payload.kind != kept.payload.kind:
        return False
    if _dedup_key(draft.content) == _dedup_key(kept.content):
        return True
    draft_ev = _dedup_key(draft.evidence[0].text) if draft.evidence else ""
    kept_ev = _dedup_key(kept.evidence[0].text) if kept.evidence else ""
    return bool(draft_ev) and bool(kept_ev) and (draft_ev in kept_ev or kept_ev in draft_ev)


def _dedupe_drafts(drafts: list[UnitDraft]) -> list[UnitDraft]:
    """Cross-chunk dedup, deterministic and LLM-free: chunk overlap makes the
    same claim show up twice at the seam — drop a later same-kind draft whose
    content normalizes identically to an earlier one, or whose primary
    evidence contains / is contained by an earlier draft's."""
    kept: list[UnitDraft] = []
    for draft in drafts:
        if any(_is_duplicate(draft, k) for k in kept):
            logger.info("CHUNK-DEDUP dropped duplicate unit: %r", draft.content)
        else:
            kept.append(draft)
    return kept


def extract_from_raw(
    store,
    episode: Episode,
    config: Config,
    on_progress: Optional[Callable[[int, int], None]] = None,
    postprocess_drafts: Optional[Callable[[list[UnitDraft]], None]] = None,
) -> list:
    """Run generic extraction over an episode's stored raw text and land the
    resulting units (method='derived', gate-enforced, atomic). Returns the
    stored units.

    Sources up to SINGLE_CALL_LIMIT chars go to the model in one call (the
    historic path, byte-identical). Longer raw is split into overlapping
    verbatim chunks (extraction/chunking.py) with one call per chunk and a
    deterministic cross-chunk dedup — chunking never touches the evidence
    gate, which validates against the FULL episode raw as always.
    `on_progress(done, total)` reports per-chunk progress when provided.
    `postprocess_drafts` runs after healing, before the store door — the hook
    document ingestion uses to stamp structural locators on evidence (it may
    annotate drafts but must never rewrite evidence text)."""
    from vetromar.ingest.generic import ingest_units

    if config.backend != "api":
        raise ConfigError(
            "Generic extraction (non-meeting sources) isn't tuned for the local model yet.",
            hint="Set VETROMAR_BACKEND=api for non-meeting ingestion; meetings work in both modes.",
        )
    if not episode.raw:
        raise ConfigError(
            f"Episode {episode.id} has no raw content to extract from.",
            hint="Re-ingest the source with its raw text attached.",
        )
    if len(episode.raw) <= SINGLE_CALL_LIMIT:
        chunks = [episode.raw]
    else:
        chunks = chunk_text(episode.raw)
        logger.info(
            "chunked extraction: %d chars -> %d chunks (episode %s)",
            len(episode.raw), len(chunks), episode.id,
        )
    drafts: list[UnitDraft] = []
    for i, chunk in enumerate(chunks):
        result = _call_model(
            config, episode, chunk,
            part=(i + 1, len(chunks)) if len(chunks) > 1 else None,
        )
        drafts.extend(
            UnitDraft(
                content=d.content,
                reasoning=d.reasoning,
                payload=d.payload,
                evidence=list(d.evidence),
            )
            for d in result.units
        )
        if on_progress is not None:
            on_progress(i + 1, len(chunks))
    if len(chunks) > 1:
        drafts = _dedupe_drafts(drafts)
    # Near-miss excerpts snap to their literal raw span before the store-door
    # gate (cheap-model tolerance; the invariant itself is untouched).
    heal_draft_evidence(drafts, derive_haystack(episode))
    if postprocess_drafts is not None:
        postprocess_drafts(drafts)
    return ingest_units(store, episode.id, drafts, method="derived", config=config)
