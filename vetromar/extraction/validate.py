"""Evidence validation — the hard gate.

Two layers, one philosophy (units are trusted because of literal evidence,
never because a model said so):

1. `validate_grounded_quotes` — the FROZEN meeting-extraction gate, checked
   before extraction output reaches ingestion. Every unit carries at least one
   grounded quote (policy 2026-07-17), and every quote is a LITERAL transcript
   span (after whitespace normalization only).
2. `validate_unit_evidence` — the universal store-door gate (engine v2),
   enforced inside `Store.add_unit`. Every Unit carries >=1 evidence item; all
   textual evidence (quote/excerpt) must be a literal span of the episode's
   raw content. The meeting haystack derivation is IDENTICAL to layer 1, so a
   unit that passes the extraction gate always passes the store door.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from vetromar.schema import ExtractedUnit, Transcript

if TYPE_CHECKING:
    from vetromar.schema import Episode, Unit


class ExtractionGateError(ValueError):
    """Base for all pre-store extraction gate failures."""


class GroundedQuoteError(ExtractionGateError):
    """Raised when an extracted quote is not a literal transcript span."""

    def __init__(self, offenders: list[str]):
        self.offenders = offenders
        detail = "\n".join(f"  - {q!r}" for q in offenders)
        super().__init__(
            "Extraction produced quotes that are NOT literal transcript spans "
            f"(paraphrase is a correctness bug):\n{detail}"
        )


class UngroundedUnitError(ExtractionGateError):
    """Raised when a unit carries no grounded quotes at all."""

    def __init__(self, decisions: list[str]):
        self.decisions = decisions
        truncated = [d if len(d) <= 100 else d[:100] + "…" for d in decisions]
        detail = "\n".join(f"  - {d!r}" for d in truncated)
        super().__init__(
            "Extraction produced units with ZERO grounded quotes "
            f"(every unit must carry literal evidence):\n{detail}"
        )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def validate_grounded_quotes(units: list[ExtractedUnit], transcript: Transcript) -> None:
    """Assert every unit carries at least one quote (UngroundedUnitError
    otherwise) and every quote is a verbatim substring of the transcript
    (GroundedQuoteError listing all offenders)."""
    ungrounded = [unit.decision for unit in units if not unit.grounded_quotes]
    if ungrounded:
        raise UngroundedUnitError(ungrounded)

    haystack = _normalize(transcript.full_text())
    offenders = [
        quote.text
        for unit in units
        for quote in unit.grounded_quotes
        if _normalize(quote.text) not in haystack
    ]
    if offenders:
        raise GroundedQuoteError(offenders)


# ---------------------------------------------------------------------------
# Universal store-door gate (engine v2)
# ---------------------------------------------------------------------------


class EvidenceMissingError(ExtractionGateError):
    """Raised when a unit carries no evidence at all — violates the store
    invariant that every unit is verifiable against its episode."""

    def __init__(self, contents: list[str]):
        self.contents = contents
        truncated = [c if len(c) <= 100 else c[:100] + "…" for c in contents]
        detail = "\n".join(f"  - {c!r}" for c in truncated)
        super().__init__(
            "Unit(s) with ZERO evidence items — every unit must carry at least "
            f"one piece of evidence (quote, excerpt, or datapoint):\n{detail}"
        )


class EvidenceMismatchError(ExtractionGateError):
    """Raised when textual evidence is not a literal span of the episode's
    raw content."""

    def __init__(self, offenders: list[str]):
        self.offenders = offenders
        detail = "\n".join(f"  - {t!r}" for t in offenders)
        super().__init__(
            "Evidence text that is NOT a literal span of the episode's raw "
            f"content (paraphrase is a correctness bug):\n{detail}"
        )


def derive_haystack(episode: "Episode") -> Optional[str]:
    """The text that textual evidence must literally appear in.

    Meeting raw is the transcript JSON — parse it and use the SAME spoken-text
    derivation as the frozen extraction gate (`Transcript.full_text()`), so the
    two gates can never disagree. Unparseable meeting raw degrades to plain
    text; other kinds use raw as-is; no raw means nothing to check against.
    """
    if episode.raw is None:
        return None
    if episode.source_kind == "meeting":
        try:
            return Transcript.model_validate_json(episode.raw).full_text()
        except ValueError:
            return episode.raw
    return episode.raw


def validate_unit_evidence(unit: "Unit", episode: "Episode") -> None:
    """The store-door invariant: >=1 evidence item, and every quote/excerpt is
    a literal (whitespace-normalized) substring of the episode's raw content.
    Datapoint evidence is structural only (shape enforced by the schema).
    Episodes without raw content skip the substring check but keep the
    >=1-evidence requirement."""
    if not unit.evidence:
        raise EvidenceMissingError([unit.content])

    raw_text = derive_haystack(episode)
    if raw_text is None:
        return
    haystack = _normalize(raw_text)
    offenders = [
        ev.text
        for ev in unit.evidence
        if ev.kind in ("quote", "excerpt") and _normalize(ev.text) not in haystack
    ]
    if offenders:
        raise EvidenceMismatchError(offenders)
