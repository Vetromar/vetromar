"""Quote healing: snap near-miss evidence back to the literal transcript span.

Cheap models routinely "clean up" quotes — dropping an interjected backchannel
("Mhmm."), fixing casing, smoothing a stutter. Under the evidence invariant
that is a paraphrase and the gate rejects the whole capture, which makes the
product's viability hinge on expensive models. This pass runs BEFORE the
(frozen, untouched) gates: for each piece of textual evidence that is not a
literal span, find the span the model obviously meant and substitute it —
verbatim, from the transcript itself. Stored evidence therefore remains
byte-literal; what changes is that a near-miss grounds to its true span
instead of failing the capture. Anything without a confident match is left
alone and still fails the gate loudly.

Healing is logged (`QUOTE-HEALED`) so calibration on real data stays visible,
same spirit as the AUTO-SUPERSEDED warnings.
"""

from __future__ import annotations

import difflib
import logging
import re

from vetromar.schema import Transcript

logger = logging.getLogger(__name__)

# Minimum similarity (SequenceMatcher ratio, healed span vs model quote) to
# accept a substitution. Below this we assume fabrication, not sloppiness.
HEAL_THRESHOLD = 0.85

# How far (chars) the true span's boundaries may drift from where the anchor
# alignment predicts them — covers a dropped interjection or a few extra words.
_BOUNDARY_SLACK = 48


def _normalize(text: str) -> str:
    # Same normalization as the gates (validate._normalize) — healing must
    # judge literality exactly the way the gate will.
    return re.sub(r"\s+", " ", text).strip()


def best_literal_span(text: str, haystack: str) -> str | None:
    """The haystack span the model most plausibly meant by `text`, or None.

    Both arguments must already be normalized. Anchors on the longest exact
    common run, then searches word boundaries near the predicted span edges
    for the window with the highest similarity to the model's text."""
    if not text or not haystack:
        return None
    sm = difflib.SequenceMatcher(None, haystack, text, autojunk=False)
    anchor = sm.find_longest_match(0, len(haystack), 0, len(text))
    # Without a substantial exact run this is a fabrication, not a near-miss.
    if anchor.size < max(15, len(text) // 3):
        return None

    # All candidate boundaries live within _BOUNDARY_SLACK of the predicted
    # span, so score against a slice around the anchor instead of the whole
    # haystack — identical results, bounded work on document-sized sources.
    window_lo = max(0, anchor.a - anchor.b - len(text) - _BOUNDARY_SLACK)
    window_hi = min(len(haystack), anchor.a - anchor.b + 2 * len(text) + _BOUNDARY_SLACK)
    offset, haystack = window_lo, haystack[window_lo:window_hi]

    expected_start = (anchor.a - offset) - anchor.b
    expected_end = expected_start + len(text)

    def word_starts(lo: int, hi: int) -> list[int]:
        lo, hi = max(0, lo), min(len(haystack), hi)
        return [i for i in range(lo, hi + 1) if i == 0 or haystack[i - 1] == " "]

    def word_ends(lo: int, hi: int) -> list[int]:
        lo, hi = max(0, lo), min(len(haystack), hi)
        return [
            i
            for i in range(lo, hi + 1)
            if i == len(haystack) or (i > 0 and haystack[i] == " ")
        ]

    starts = word_starts(expected_start - _BOUNDARY_SLACK, expected_start + _BOUNDARY_SLACK)
    ends = word_ends(expected_end - _BOUNDARY_SLACK, expected_end + _BOUNDARY_SLACK)

    scorer = difflib.SequenceMatcher(None, autojunk=False)
    scorer.set_seq2(text)
    best_ratio, best_span = 0.0, None
    for s in starts:
        for e in ends:
            if e <= s:
                continue
            candidate = haystack[s:e].strip()
            scorer.set_seq1(candidate)
            if scorer.quick_ratio() <= best_ratio:
                continue
            ratio = scorer.ratio()
            if ratio > best_ratio:
                best_ratio, best_span = ratio, candidate
    return best_span if best_ratio >= HEAL_THRESHOLD else None


def _heal_texts(items, haystack_norm: str) -> list[tuple[str, str]]:
    """Heal `.text` on each item in place; returns (original, healed) pairs."""
    healed: list[tuple[str, str]] = []
    for item in items:
        text_norm = _normalize(item.text)
        if text_norm in haystack_norm:
            continue
        span = best_literal_span(text_norm, haystack_norm)
        if span is not None:
            logger.warning("QUOTE-HEALED: %r -> %r", item.text, span)
            healed.append((item.text, span))
            item.text = span
    return healed


def heal_grounded_quotes(units, transcript: Transcript) -> list[tuple[str, str]]:
    """Meeting path: heal every unit's grounded quotes against the transcript
    (the same haystack the frozen gate uses). Mutates quotes in place."""
    haystack = _normalize(transcript.full_text())
    return _heal_texts(
        (q for unit in units for q in unit.grounded_quotes), haystack
    )


def heal_draft_evidence(drafts, haystack_text: str | None) -> list[tuple[str, str]]:
    """Generic path: heal quote/excerpt evidence on UnitDrafts against the
    episode's derived haystack (see validate.derive_haystack)."""
    if not haystack_text:
        return []
    haystack = _normalize(haystack_text)
    return _heal_texts(
        (
            ev
            for draft in drafts
            for ev in draft.evidence
            if ev.kind in ("quote", "excerpt")
        ),
        haystack,
    )
