"""The context block: fused, token-budgeted, citation-tagged markdown from
the store — sections, budget trimming, change lineage, and the MCP/HTTP
surfaces returning the same shape."""

from vetromar.context import build_context
from vetromar.ingest.manual import (
    add_draft,
    add_source_episode,
    create_entity,
    supersede,
)
from vetromar.schema import ClaimPayload, ExcerptEvidence, UnitDraft


def _claim(store, episode_id, text):
    return add_draft(store, episode_id, UnitDraft(
        content=text, payload=ClaimPayload(), evidence=[ExcerptEvidence(text=text)],
    ))


def _seed(store):
    ep = add_source_episode(store, title="Thread")
    old = _claim(store, ep.id, "Billing stays on the monolith for Q3")
    new = _claim(store, ep.id, "Billing moves off the monolith in Q4")
    entity = create_entity(store, "Priya K")
    store.update_entity_profile(entity.id, summary="Payments lead")
    store.add_edge(new.id, entity.id, kind="mentions")
    store.add_edge(new.id, old.id, kind="supersedes")
    supersede(store, old.id, new.id)
    return old, new, entity


def test_context_block_sections_and_citations(store):
    old, new, entity = _seed(store)
    result = build_context(store, "billing monolith")

    assert new.id in result["citations"]
    context = result["context"]
    assert context.startswith("# Context: billing monolith")
    assert "## Current facts" in context
    assert f"[{new.id}]" in context
    # superseded unit is NOT a current fact but shows up as change lineage
    assert "## Changed or contradicted" in context
    assert "superseded" in context
    # the mentioned entity's card appears with its summary
    assert "Priya K (person): Payments lead" in context
    # verbatim evidence with citation
    assert "## Evidence excerpts" in context


def test_context_block_respects_token_budget(store):
    ep = add_source_episode(store, title="Thread")
    for i in range(40):
        _claim(store, ep.id, f"Observation number {i} about the billing system rework")
    small = build_context(store, "billing system", token_budget=150)
    large = build_context(store, "billing system", token_budget=4000)
    assert len(small["context"]) < len(large["context"])
    assert len(small["context"]) <= 150 * 4 + 200  # header slack


def test_context_block_empty_store(store):
    result = build_context(store, "anything at all")
    assert result["citations"] == []
    assert "no matching knowledge" in result["context"]


def test_context_http_route(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    from vetromar.store import Store
    from vetromar.ui_server.app import create_app

    store = Store(tmp_path / "store.db")
    _seed(store)
    store.close()

    client = TestClient(create_app())
    resp = client.get("/api/store/context", params={"query": "billing monolith"})
    assert resp.status_code == 200
    body = resp.json()
    assert "## Current facts" in body["context"]
    assert body["citations"]
