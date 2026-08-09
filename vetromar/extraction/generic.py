"""Generic extraction: raw text of ANY source kind -> typed UnitDrafts.

API backend only in v1 — the local model's grammar path took eleven tuning
rounds for the meeting schema and hasn't been tuned for this one; a clean
ConfigError beats silently bad extraction. The meeting pipeline is unaffected
(it has its own frozen extractor).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction.repair import heal_draft_evidence
from vetromar.extraction.validate import derive_haystack
from vetromar.extraction.generic_prompt import (
    GENERIC_SYSTEM_PROMPT,
    build_generic_user_prompt,
)
from vetromar.schema import Episode, ExcerptEvidence, UnitDraft, UnitPayload


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


def _call_model(config: Config, episode: Episode) -> GenericExtractionResult:
    from vetromar.ai import get_provider, map_ai_error

    provider = get_provider(config)
    try:
        return provider.parse_structured(
            system=GENERIC_SYSTEM_PROMPT,
            user=build_generic_user_prompt(
                episode.source_kind, episode.title, episode.raw or ""
            ),
            schema=GenericExtractionResult,
            max_tokens=16000,
        )
    except Exception as exc:
        mapped = map_ai_error(exc, config)
        if mapped is not None:
            raise mapped from exc
        raise


def extract_from_raw(store, episode: Episode, config: Config) -> list:
    """Run generic extraction over an episode's stored raw text and land the
    resulting units (method='derived', gate-enforced, atomic). Returns the
    stored units."""
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
    result = _call_model(config, episode)
    drafts = [
        UnitDraft(
            content=d.content,
            reasoning=d.reasoning,
            payload=d.payload,
            evidence=list(d.evidence),
        )
        for d in result.units
    ]
    # Near-miss excerpts snap to their literal raw span before the store-door
    # gate (cheap-model tolerance; the invariant itself is untouched).
    heal_draft_evidence(drafts, derive_haystack(episode))
    return ingest_units(store, episode.id, drafts, method="derived", config=config)
