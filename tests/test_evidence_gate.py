"""The universal store-door gate: >=1 evidence always; textual evidence is a
literal span of the episode's raw; the meeting derivation agrees byte-for-byte
with the frozen extraction gate. Plus the v2 schema-version guard and batch
atomicity."""

from datetime import datetime, timezone

import pytest

from tests.conftest import make_billing_unit
from vetromar.errors import ConfigError
from vetromar.extraction.validate import (
    EvidenceMismatchError,
    EvidenceMissingError,
    derive_haystack,
    validate_grounded_quotes,
    validate_unit_evidence,
)
from vetromar.ingest.map import unit_from_draft, unit_from_extracted
from vetromar.ingest.manual import add_source_episode
from vetromar.schema import (
    ClaimPayload,
    DataPointEvidence,
    Episode,
    ExcerptEvidence,
    MetricPayload,
    UnitDraft,
)
from vetromar.store import Store

WHEN = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _claim(text, evidence):
    draft = UnitDraft(content=text, payload=ClaimPayload(), evidence=evidence)
    return unit_from_draft(draft, episode_id="ep_x", method="concierge", valid_from=WHEN)


def _episode(raw=None, source_kind="note"):
    return Episode(id="ep_x", source_kind=source_kind, title="t", occurred_at=WHEN, raw=raw)


# -- the invariant matrix ----------------------------------------------------


def test_zero_evidence_rejected_even_without_raw():
    unit = _claim("Bare assertion", evidence=[])
    with pytest.raises(EvidenceMissingError) as exc:
        validate_unit_evidence(unit, _episode(raw=None))
    assert "Bare assertion" in str(exc.value)


def test_excerpt_must_be_literal_span_of_raw():
    raw = "Marta wrote: we are moving the launch to September."
    ok = _claim("Launch moved", [ExcerptEvidence(text="moving the launch to September")])
    validate_unit_evidence(ok, _episode(raw=raw))

    para = _claim("Launch moved", [ExcerptEvidence(text="the launch was postponed")])
    with pytest.raises(EvidenceMismatchError):
        validate_unit_evidence(para, _episode(raw=raw))


def test_whitespace_normalization_matches_frozen_gate():
    raw = "we are  moving\nthe launch"
    unit = _claim("Launch moved", [ExcerptEvidence(text="moving the launch")])
    validate_unit_evidence(unit, _episode(raw=raw))


def test_no_raw_skips_substring_but_keeps_presence():
    unit = _claim("Claim", [ExcerptEvidence(text="anything at all")])
    validate_unit_evidence(unit, _episode(raw=None))  # no raise


def test_datapoint_evidence_is_structural_only():
    draft = UnitDraft(
        content="Signups dropped",
        payload=MetricPayload(metric="signups", value="812"),
        evidence=[DataPointEvidence(description="daily signups", value="812", at=WHEN)],
    )
    unit = unit_from_draft(draft, episode_id="ep_x", method="pushed", valid_from=WHEN)
    # raw present but the value string isn't in it — datapoints aren't text-gated
    validate_unit_evidence(unit, _episode(raw="unrelated raw payload"))


# -- meeting-haystack parity with the frozen gate ----------------------------


def test_meeting_haystack_is_frozen_gate_derivation(billing_transcript):
    episode = _episode(raw=billing_transcript.model_dump_json(), source_kind="meeting")
    assert derive_haystack(episode) == billing_transcript.full_text()


def test_frozen_gate_pass_implies_store_gate_pass(billing_transcript):
    """The regression trap: a unit that clears validate_grounded_quotes must
    clear validate_unit_evidence on its ingested meeting episode."""
    eu = make_billing_unit()
    validate_grounded_quotes([eu], billing_transcript)  # frozen gate: passes
    unit = unit_from_extracted(eu, episode_id="ep_x", method="captured", valid_from=WHEN)
    episode = _episode(raw=billing_transcript.model_dump_json(), source_kind="meeting")
    validate_unit_evidence(unit, episode)  # store door: must also pass


def test_unparseable_meeting_raw_degrades_to_plain_text():
    episode = _episode(raw="not json, just words about the launch", source_kind="meeting")
    assert derive_haystack(episode) == "not json, just words about the launch"
    unit = _claim("Launch", [ExcerptEvidence(text="words about the launch")])
    validate_unit_evidence(unit, episode)


# -- enforcement at the store door -------------------------------------------


def test_store_add_unit_enforces_gate(store):
    ep = add_source_episode(store, title="Thread", raw="we agreed to ship v2 on friday")
    good = _claim("Ship v2 friday", [ExcerptEvidence(text="ship v2 on friday")])
    good.provenance.episode_id = ep.id
    store.add_unit(good)

    bad = _claim("Ship v2 friday", [ExcerptEvidence(text="shipping got agreed")])
    bad.provenance.episode_id = ep.id
    with pytest.raises(EvidenceMismatchError):
        store.add_unit(bad)
    assert len(store.list_units()) == 1


def test_add_units_batch_is_atomic(store):
    ep = add_source_episode(store, title="Thread", raw="we agreed to ship v2 on friday")
    good = _claim("A", [ExcerptEvidence(text="ship v2")])
    offender = _claim("The offending unit", [])
    for u in (good, offender):
        u.provenance.episode_id = ep.id

    with pytest.raises(EvidenceMissingError) as exc:
        store.add_units([good, offender])
    assert "The offending unit" in str(exc.value)
    assert store.list_units() == []  # nothing landed — all-or-nothing


# -- schema version guard ----------------------------------------------------


def test_old_schema_db_gets_config_error(tmp_path):
    import sqlite3

    old_db = tmp_path / "store.db"
    conn = sqlite3.connect(old_db)
    conn.execute("CREATE TABLE units (id TEXT PRIMARY KEY)")  # v1-era table, user_version 0
    conn.commit()
    conn.close()

    with pytest.raises(ConfigError) as exc:
        Store(old_db)
    assert "delete" in (exc.value.hint or "").lower()


def test_fresh_and_v2_dbs_open_fine(tmp_path):
    db = tmp_path / "store.db"
    Store(db).close()   # fresh file: creates v2
    Store(db).close()   # reopening a v2 store is fine
