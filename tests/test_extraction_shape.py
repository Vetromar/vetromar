"""Both backends sit behind one interface and one schema; structure is
guaranteed at the boundary regardless of which produced the units."""

import json

import pytest

from tests.conftest import make_billing_unit
from vetromar.config import Config
from vetromar.extraction.base import ExtractionBackend, make_backend
from vetromar.schema import ExtractedUnit, ExtractionResult, Transcript


def test_backend_factory_selects_by_config():
    # Backends construct lazily/importably; api needs the anthropic package only.
    assert type(make_backend(Config(backend="local", local_model="x"))).__name__ == "LocalBackend"
    with pytest.raises(ValueError):
        make_backend(Config(backend="cloud-magic"))


def test_frozen_extraction_schema_snapshot():
    """The meeting extraction schema is FROZEN: it drives the tuned Ollama
    grammar (`_UNIT_FIELD_ORDER`) and the Anthropic output_format. If this
    hash moves, someone touched ExtractedUnit/ExtractionResult or a nested
    frozen model — revert unless that was an explicit, maintainer-approved
    retune (then update the hash here)."""
    import hashlib

    canon = json.dumps(ExtractionResult.model_json_schema(), sort_keys=True)
    assert (
        hashlib.sha256(canon.encode()).hexdigest()
        == "e1f82560cc86cc09dfdb88d9929d0f8edb4b21484655b5c4534ab598db0b3797"
    )


def test_extraction_result_schema_exports_for_both_backends():
    """One Pydantic schema drives the Anthropic output_format AND the Ollama
    format constraint — verify it exports and constrains the right fields."""
    schema = ExtractionResult.model_json_schema()
    assert "units" in schema["properties"]
    unit_props = schema["$defs"]["ExtractedUnit"]["properties"]
    for field in (
        "decision", "reasoning", "rejected_alternatives",
        "advocate", "objectors", "status", "grounded_quotes",
    ):
        assert field in unit_props, f"schema lost field: {field}"
    # the model must never be asked to produce identity/provenance fields
    for forbidden in ("id", "provenance", "valid_from", "valid_to", "ingested_at"):
        assert forbidden not in unit_props
    assert set(schema["$defs"]["Status"]["enum"]) == {"Decided", "Leaning", "Parked"}


def test_status_flattening_is_impossible():
    """Schema rejects any status outside the tri-state — 'decided-ish' can't ship."""
    unit = make_billing_unit().model_dump()
    unit["status"] = "Probably"
    with pytest.raises(ValueError):
        ExtractedUnit.model_validate(unit)


class StubBackend(ExtractionBackend):
    """Deterministic backend standing in for either real backend — used to
    exercise everything downstream of the extraction seam without a model."""

    def __init__(self, units):
        self._units = units

    def extract(self, transcript: Transcript):
        return self._units


def test_stub_backend_round_trips_through_json():
    """Simulate the wire: model emits JSON -> validates into ExtractedUnits."""
    raw = json.dumps({"units": [json.loads(make_billing_unit().model_dump_json())]})
    result = ExtractionResult.model_validate_json(raw)
    assert len(result.units) == 1
    assert result.units[0].advocate.ref == "SPEAKER_02"
    assert result.units[0].objectors[0].grounds.startswith("Two migrations")


def test_pipeline_downstream_of_extraction(store, billing_transcript, monkeypatch):
    """Stages 4-6 end-to-end with a stubbed backend: extract -> validate ->
    store -> render. Downstream is identical for both real backends."""
    from vetromar.capture import pipeline as pl

    stub = StubBackend([make_billing_unit()])
    monkeypatch.setattr(pl, "make_backend", lambda config: stub)

    episode, units, markdown = pl.run_from_transcript(
        billing_transcript, title="Architecture review",
        config=Config(backend="local"), store=store,
    )
    assert store.get_unit(units[0].id).provenance.episode_id == episode.id
    assert "# Architecture review" in markdown
    assert "[Decided] Move billing off the monolith" in markdown
    assert "Decision: billing comes off the monolith" in markdown  # grounded quote rendered


def test_pipeline_rejects_paraphrase_before_store(store, billing_transcript, monkeypatch):
    from vetromar.capture import pipeline as pl
    from vetromar.extraction.validate import GroundedQuoteError
    from vetromar.schema import GroundedQuote, PersonRef

    bad = make_billing_unit()
    bad.grounded_quotes[0] = GroundedQuote(
        text="we all agreed billing should be split", speaker=PersonRef(ref="X"),
        start_ms=0, end_ms=1,
    )
    monkeypatch.setattr(pl, "make_backend", lambda config: StubBackend([bad]))

    with pytest.raises(GroundedQuoteError):
        pl.run_from_transcript(
            billing_transcript, title="t", config=Config(backend="local"), store=store,
        )
    assert store.list_units() == []  # nothing leaked into the store


def test_pipeline_rejects_ungrounded_unit_before_store(store, billing_transcript, monkeypatch):
    from vetromar.capture import pipeline as pl
    from vetromar.extraction.validate import UngroundedUnitError

    bad = make_billing_unit()
    bad.grounded_quotes = []
    monkeypatch.setattr(pl, "make_backend", lambda config: StubBackend([bad]))

    with pytest.raises(UngroundedUnitError):
        pl.run_from_transcript(
            billing_transcript, title="t", config=Config(backend="local"), store=store,
        )
    assert store.list_units() == []
