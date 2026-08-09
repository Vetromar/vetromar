"""Quote healing: near-miss evidence snaps to its literal span; fabrication
still fails the (untouched) gates."""

import pytest

from tests.conftest import make_billing_unit
from vetromar.extraction.repair import (
    best_literal_span,
    heal_draft_evidence,
    heal_grounded_quotes,
)
from vetromar.extraction.validate import GroundedQuoteError, validate_grounded_quotes
from vetromar.schema import (
    ExcerptEvidence,
    GroundedQuote,
    PersonRef,
    Transcript,
    TranscriptSegment,
)


def _seg(text, speaker="SPEAKER_00", start=0, end=1000):
    return TranscriptSegment(speaker=speaker, text=text, start_ms=start, end_ms=end)


def _unit_with_quote(text):
    unit = make_billing_unit()
    unit.grounded_quotes = [
        GroundedQuote(text=text, speaker=PersonRef(ref="SPEAKER_00"), start_ms=0, end_ms=1)
    ]
    return unit


# The first real-world gate failure (2026-07-19, YC podcast via Deepgram +
# Haiku): the transcript carries an inline backchannel ("Mhmm.") that the
# model cleaned out of its quote, plus the casing fix that implies.
REAL_SEGMENTS = [
    _seg("one of them is by default, the agent conversation is actually"),
    _seg(
        "globally view viewable by any full time employee at YC. You know, we "
        "sort of weren't sure about Mhmm. That decision. I mean, it felt right "
        "and it felt like living in the future,"
    ),
    _seg("but it did not come easily. I feel like we had a lot of conversations about like,"),
]
REAL_QUOTE = (
    "by default, the agent conversation is actually globally view viewable by "
    "any full time employee at YC. You know, we sort of weren't sure about "
    "that decision. I mean, it felt right and it felt like living in the "
    "future, but it did not come easily."
)


def test_real_world_backchannel_case_heals_and_passes_gate():
    transcript = Transcript(segments=REAL_SEGMENTS)
    unit = _unit_with_quote(REAL_QUOTE)

    healed = heal_grounded_quotes([unit], transcript)

    assert len(healed) == 1
    assert "Mhmm. That decision" in unit.grounded_quotes[0].text
    # The healed quote is now verbatim — the frozen gate must accept it.
    validate_grounded_quotes([unit], transcript)


def test_literal_quotes_left_untouched():
    transcript = Transcript(segments=[_seg("we decided to ship the beta on Friday")])
    unit = _unit_with_quote("we decided to ship the beta on Friday")
    assert heal_grounded_quotes([unit], transcript) == []
    assert unit.grounded_quotes[0].text == "we decided to ship the beta on Friday"


def test_casing_and_dropped_word_heal():
    transcript = Transcript(
        segments=[_seg("So, um, we will move the billing service off the monolith next quarter")]
    )
    unit = _unit_with_quote("we will move the billing service off the monolith next quarter")
    heal_grounded_quotes([unit], transcript)
    validate_grounded_quotes([unit], transcript)


def test_fabricated_quote_is_not_healed_and_still_fails():
    transcript = Transcript(segments=[_seg("today we talked about hiring and the offsite")])
    unit = _unit_with_quote("we agreed to migrate the data warehouse to BigQuery")
    assert heal_grounded_quotes([unit], transcript) == []
    with pytest.raises(GroundedQuoteError):
        validate_grounded_quotes([unit], transcript)


def test_low_similarity_span_rejected_by_threshold():
    # Shares an anchor ("the quarterly report") but the rest diverges — the
    # model invented most of it, so healing must refuse.
    haystack = "Priya said the quarterly report is late again and blamed the vendor"
    text = (
        "the quarterly report shows revenue doubled and churn dropped to two "
        "percent across every region we operate in"
    )
    assert best_literal_span(text, haystack) is None


def test_draft_excerpt_evidence_heals_against_raw():
    class Draft:
        def __init__(self, text):
            self.evidence = [ExcerptEvidence(text=text, author=None)]

    raw = "From: sam@x.com\nWe are going with, uh, vendor B for the rollout.\nThanks"
    draft = Draft("We are going with vendor B for the rollout.")
    healed = heal_draft_evidence([draft], raw)
    assert len(healed) == 1
    assert draft.evidence[0].text == "We are going with, uh, vendor B for the rollout."


def test_draft_healing_skips_missing_haystack():
    class Draft:
        def __init__(self):
            self.evidence = [ExcerptEvidence(text="anything", author=None)]

    assert heal_draft_evidence([Draft()], None) == []
