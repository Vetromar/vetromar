"""Grounded quotes are LITERAL — paraphrase is a correctness bug and must
fail loudly before anything reaches the store."""

import pytest

from tests.conftest import make_billing_unit
from vetromar.extraction.validate import (
    GroundedQuoteError,
    UngroundedUnitError,
    validate_grounded_quotes,
)
from vetromar.schema import GroundedQuote, PersonRef


def test_literal_quote_passes(billing_transcript):
    validate_grounded_quotes([make_billing_unit()], billing_transcript)


def test_paraphrased_quote_fails(billing_transcript):
    unit = make_billing_unit()
    unit.grounded_quotes[0] = GroundedQuote(
        text="We decided to split billing out of the monolith",  # paraphrase!
        speaker=PersonRef(ref="SPEAKER_00"),
        start_ms=70400,
        end_ms=79000,
    )
    with pytest.raises(GroundedQuoteError) as exc:
        validate_grounded_quotes([unit], billing_transcript)
    assert "split billing out" in str(exc.value)


def test_whitespace_normalization_tolerated(billing_transcript):
    unit = make_billing_unit()
    # Same words, different whitespace — still literal, should pass.
    unit.grounded_quotes[0].text = (
        "Decision:  billing comes off the monolith,\n invoicing service first, "
        "kickoff after the data layer freeze."
    )
    validate_grounded_quotes([unit], billing_transcript)


def test_trimmed_inner_words_fail(billing_transcript):
    unit = make_billing_unit()
    unit.grounded_quotes[0].text = (
        "Decision: billing comes off the monolith, kickoff after the data layer freeze."
    )  # dropped "invoicing service first," — not a literal span
    with pytest.raises(GroundedQuoteError):
        validate_grounded_quotes([unit], billing_transcript)


def test_all_offenders_reported(billing_transcript):
    unit = make_billing_unit()
    unit.grounded_quotes = [
        GroundedQuote(text="fabricated one", speaker=PersonRef(ref="A"), start_ms=0, end_ms=1),
        GroundedQuote(text="fabricated two", speaker=PersonRef(ref="B"), start_ms=1, end_ms=2),
    ]
    with pytest.raises(GroundedQuoteError) as exc:
        validate_grounded_quotes([unit], billing_transcript)
    assert exc.value.offenders == ["fabricated one", "fabricated two"]


def test_zero_quote_unit_fails_gate(billing_transcript):
    unit = make_billing_unit()
    unit.grounded_quotes = []
    with pytest.raises(UngroundedUnitError) as exc:
        validate_grounded_quotes([unit], billing_transcript)
    assert unit.decision[:50] in str(exc.value)


def test_mixed_empty_and_literal_units(billing_transcript):
    grounded = make_billing_unit()
    empty = make_billing_unit()
    empty.decision = "Ship it with no evidence"
    empty.grounded_quotes = []
    with pytest.raises(UngroundedUnitError) as exc:
        validate_grounded_quotes([grounded, empty], billing_transcript)
    assert exc.value.decisions == ["Ship it with no evidence"]
