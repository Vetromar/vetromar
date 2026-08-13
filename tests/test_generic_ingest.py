"""The generic ingestion surface: raw episodes + gate-enforced atomic unit
batches, the API-only generic extractor, and the CLI/MCP write wiring."""

import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction import generic as generic_mod
from vetromar.extraction.generic import GenericDraft, GenericExtractionResult, extract_from_raw
from vetromar.extraction.validate import EvidenceMismatchError
from vetromar.ingest.generic import ingest_episode, ingest_units
from vetromar.mcp_server import server as srv
from vetromar.schema import ClaimPayload, CommitmentPayload, ExcerptEvidence, PersonRef, UnitDraft

RAW = (
    "marta: heads up — we're moving the launch to September, the auth rewrite isn't done.\n"
    "jonas: ok. I'll own updating the public roadmap by Friday.\n"
)
WHEN = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)


def _episode(store):
    return ingest_episode(
        store, title="Slack #launch", source_kind="chat", raw=RAW, occurred_at=WHEN
    )


def _draft(content, excerpt, author=None, payload=None):
    return UnitDraft(
        content=content,
        payload=payload or ClaimPayload(),
        evidence=[ExcerptEvidence(text=excerpt, author=PersonRef(ref=author) if author else None)],
    )


def test_ingest_units_lands_batch_with_provenance(store):
    ep = _episode(store)
    stored = ingest_units(
        store,
        ep.id,
        [
            _draft("Launch moved to September", "moving the launch to September", author="marta"),
            _draft(
                "Jonas owns the roadmap update",
                "I'll own updating the public roadmap by Friday",
                author="jonas",
                payload=CommitmentPayload(owner=PersonRef(ref="jonas")),
            ),
        ],
        method="pushed",
        agent="acme-agent",
    )
    assert [u.type for u in stored] == ["claim", "commitment"]
    got = store.get_unit(stored[0].id)
    assert got.provenance.method == "pushed"
    assert got.provenance.agent == "acme-agent"
    assert got.valid_from == WHEN  # facts hold from the source event


def test_ingest_units_batch_atomic_with_offender_detail(store):
    ep = _episode(store)
    good = _draft("Launch moved", "moving the launch to September")
    bad = _draft("Roadmap owner", "jonas will handle the roadmap soon")  # paraphrase

    with pytest.raises(EvidenceMismatchError) as exc:
        ingest_units(store, ep.id, [good, bad])
    assert "jonas will handle the roadmap soon" in str(exc.value)
    assert store.list_units() == []  # all-or-nothing


def test_extract_from_raw_local_backend_refused(store):
    ep = _episode(store)
    with pytest.raises(ConfigError) as exc:
        extract_from_raw(store, ep, Config(backend="local"))
    assert "local model" in exc.value.message


def test_extract_from_raw_api_path_lands_derived_units(store, monkeypatch):
    ep = _episode(store)
    result = GenericExtractionResult(units=[
        GenericDraft(
            content="Launch moved to September",
            reasoning="the auth rewrite isn't done",
            payload=ClaimPayload(),
            evidence=[ExcerptEvidence(text="moving the launch to September", author=PersonRef(ref="marta"))],
        )
    ])
    monkeypatch.setattr(
        generic_mod, "_call_model", lambda config, episode, text, part=None: result
    )

    units = extract_from_raw(store, ep, Config(backend="api", api_key="sk-test"))
    assert len(units) == 1
    assert units[0].provenance.method == "derived"
    assert store.get_unit(units[0].id).reasoning == "the auth rewrite isn't done"


def test_extract_from_raw_requires_raw(store):
    ep = ingest_episode(store, title="No raw", source_kind="note")
    with pytest.raises(ConfigError):
        extract_from_raw(store, ep, Config(backend="api", api_key="sk-test"))


def test_cli_ingest_wires_episode_and_extraction(tmp_path, monkeypatch):
    from vetromar.cli import app

    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    source = tmp_path / "thread.txt"
    source.write_text(RAW)

    seen = {}

    def fake_extract(store, episode, config):
        seen["episode"] = episode
        return []

    monkeypatch.setattr(generic_mod, "extract_from_raw", fake_extract)

    result = CliRunner().invoke(
        app, ["ingest", str(source), "--title", "Slack #launch", "--kind", "chat"]
    )
    assert result.exit_code == 0, result.output
    assert seen["episode"].source_kind == "chat"
    assert seen["episode"].raw == RAW


def test_mcp_write_tools_round_trip(store, monkeypatch):
    monkeypatch.setattr(srv, "_get_store", lambda graph=None: store)
    # Hermetic: never read the real ~/.vetromar config (whose api key would
    # turn auto-linking into live LLM calls inside a test).
    monkeypatch.setattr(srv, "load_config", lambda: Config(backend="local"))

    ep = srv.ingest_episode(
        title="Slack #launch", source_kind="chat", raw=RAW,
        occurred_at="2026-07-02T09:00:00+00:00",
    )
    assert ep["source_kind"] == "chat"
    assert "raw" not in ep  # never echoed back

    stored = srv.add_units(ep["id"], [json.loads(_draft(
        "Launch moved to September", "moving the launch to September", author="marta",
    ).model_dump_json())], agent="acme-agent")
    assert stored[0]["unit"]["provenance"]["method"] == "pushed"
    assert stored[0]["unit"]["provenance"]["agent"] == "acme-agent"

    # a second pushed unit, then link + supersede through the write surface
    second = srv.add_units(ep["id"], [json.loads(_draft(
        "Roadmap update owned by jonas", "updating the public roadmap by Friday",
    ).model_dump_json())])
    edge = srv.link_units(stored[0]["unit"]["id"], second[0]["unit"]["id"], kind="spawned")
    assert edge["method"] == "pushed"

    closed = srv.supersede_unit(stored[0]["unit"]["id"], second[0]["unit"]["id"])
    assert closed["unit"]["valid_to"] is not None
    assert any(e["kind"] == "supersedes" for e in closed["edges"])


def test_mcp_add_units_gate_failure_names_offender(store, monkeypatch):
    monkeypatch.setattr(srv, "_get_store", lambda graph=None: store)
    ep = srv.ingest_episode(title="T", source_kind="chat", raw=RAW)

    with pytest.raises(EvidenceMismatchError) as exc:
        srv.add_units(ep["id"], [json.loads(_draft(
            "Launch moved", "the launch got pushed back a quarter",
        ).model_dump_json())])
    assert "the launch got pushed back a quarter" in str(exc.value)
    assert store.list_units() == []