"""Schema v2: discriminated unions round-trip, the computed `type` field, and
the frozen-extraction -> universal-Unit mapping."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tests.conftest import make_billing_unit
from vetromar.ingest.map import unit_from_draft, unit_from_extracted
from vetromar.schema import (
    ClaimPayload,
    CommitmentPayload,
    DataPointEvidence,
    DecisionPayload,
    ExcerptEvidence,
    MetricPayload,
    PersonRef,
    Provenance,
    QuestionPayload,
    QuoteEvidence,
    Status,
    Unit,
    UnitDraft,
)

WHEN = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _unit(payload, evidence=None) -> Unit:
    return Unit(
        content="c",
        payload=payload,
        evidence=evidence or [ExcerptEvidence(text="c")],
        provenance=Provenance(method="concierge", episode_id="ep_x"),
    )


@pytest.mark.parametrize(
    "payload",
    [
        DecisionPayload(status=Status.LEANING, advocate=PersonRef(ref="Priya")),
        ClaimPayload(),
        CommitmentPayload(owner=PersonRef(ref="Marta"), due=WHEN),
        QuestionPayload(raised_by=PersonRef(ref="SPEAKER_01")),
        MetricPayload(metric="signup conversion", value="12.5", unit="%", at=WHEN, source_system="posthog"),
    ],
)
def test_payload_union_round_trips(payload):
    unit = _unit(payload)
    back = Unit.model_validate_json(unit.model_dump_json())
    assert type(back.payload) is type(payload)
    assert back.payload == payload
    assert back.type == payload.kind


@pytest.mark.parametrize(
    "evidence",
    [
        QuoteEvidence(text="t", speaker=PersonRef(ref="SPEAKER_00"), start_ms=0, end_ms=10),
        ExcerptEvidence(text="t", author=PersonRef(ref="marta@co"), locator="msg-3"),
        DataPointEvidence(description="signups query", value="812", at=WHEN, locator="rows[0]"),
    ],
)
def test_evidence_union_round_trips(evidence):
    unit = _unit(ClaimPayload(), evidence=[evidence])
    back = Unit.model_validate_json(unit.model_dump_json())
    assert type(back.evidence[0]) is type(evidence)
    assert back.evidence[0] == evidence


def test_computed_type_serializes_and_is_tolerated_on_input():
    unit = _unit(ClaimPayload())
    dumped = unit.model_dump()
    assert dumped["type"] == "claim"
    # a round-trip carrying the (computed) type key must validate cleanly
    assert Unit.model_validate(dumped).type == "claim"


def test_unknown_payload_kind_rejected():
    with pytest.raises(ValidationError):
        UnitDraft.model_validate({"content": "c", "payload": {"kind": "vibe"}})


def test_unit_from_extracted_maps_all_frozen_fields():
    eu = make_billing_unit()
    unit = unit_from_extracted(eu, episode_id="ep_1", method="captured", valid_from=WHEN)

    assert unit.type == "decision"
    assert unit.content == eu.decision
    assert unit.reasoning == eu.reasoning
    assert unit.payload.status == eu.status
    assert unit.payload.advocate == eu.advocate
    assert unit.payload.objectors == eu.objectors
    assert unit.payload.rejected_alternatives == eu.rejected_alternatives
    assert len(unit.evidence) == len(eu.grounded_quotes)
    q, ev = eu.grounded_quotes[0], unit.evidence[0]
    assert ev.kind == "quote"
    assert (ev.text, ev.speaker, ev.start_ms, ev.end_ms) == (q.text, q.speaker, q.start_ms, q.end_ms)
    assert unit.provenance.method == "captured"
    assert unit.provenance.episode_id == "ep_1"
    assert unit.valid_from == WHEN


def test_unit_from_draft_carries_agent():
    draft = UnitDraft(content="c", payload=ClaimPayload(), evidence=[ExcerptEvidence(text="c")])
    unit = unit_from_draft(
        draft, episode_id="ep_1", method="pushed", valid_from=WHEN, agent="acme-agent"
    )
    assert unit.provenance.method == "pushed"
    assert unit.provenance.agent == "acme-agent"


def test_unit_draft_validates_from_plain_json():
    draft = UnitDraft.model_validate(
        {
            "content": "Signups dropped after the pricing change",
            "payload": {"kind": "metric", "metric": "signups", "value": "812"},
            "evidence": [
                {"kind": "datapoint", "description": "daily signups", "value": "812",
                 "at": "2026-07-01T10:00:00+00:00"}
            ],
        }
    )
    assert draft.payload.kind == "metric"
    assert draft.evidence[0].kind == "datapoint"
