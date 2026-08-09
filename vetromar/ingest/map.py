"""Extraction output -> store node mapping.

The meeting extractor's `ExtractedUnit` (FROZEN shape) and the generalized
`UnitDraft` both become the same universal `Unit` here — this file is the seam
that lets extraction schemas stay tuned per source while the store keeps one
node shape.
"""

from __future__ import annotations

from datetime import datetime

from vetromar.schema import (
    DecisionPayload,
    ExtractedUnit,
    Provenance,
    QuoteEvidence,
    Unit,
    UnitDraft,
)


def unit_from_extracted(
    eu: ExtractedUnit,
    *,
    episode_id: str,
    valid_from: datetime,
    method: str = "captured",
    agent: str | None = None,
) -> Unit:
    """A meeting extraction becomes a decision-typed Unit: the decision text is
    the claim, deliberation context is the payload, grounded quotes are the
    evidence."""
    return Unit(
        content=eu.decision,
        reasoning=eu.reasoning,
        payload=DecisionPayload(
            status=eu.status,
            advocate=eu.advocate,
            objectors=eu.objectors,
            rejected_alternatives=eu.rejected_alternatives,
        ),
        evidence=[QuoteEvidence(**q.model_dump()) for q in eu.grounded_quotes],
        provenance=Provenance(method=method, episode_id=episode_id, agent=agent),
        valid_from=valid_from,
    )


def unit_from_draft(
    draft: UnitDraft,
    *,
    episode_id: str,
    valid_from: datetime,
    method: str,
    agent: str | None = None,
) -> Unit:
    """A pushed/concierge/derived draft becomes a Unit — the store assigns
    identity and provenance; the caller never does."""
    return Unit(
        content=draft.content,
        reasoning=draft.reasoning,
        payload=draft.payload,
        evidence=draft.evidence,
        provenance=Provenance(method=method, episode_id=episode_id, agent=agent),
        valid_from=valid_from,
    )
