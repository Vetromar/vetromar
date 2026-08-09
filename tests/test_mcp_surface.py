"""The MCP read surface: read-only (this phase), provenance- and
edge-carrying payloads, raw kept out of listings."""

from datetime import datetime, timezone

from tests.conftest import make_billing_unit
from vetromar.ingest import ingest_room
from vetromar.ingest.manual import add_draft, add_source_episode, create_entity, link_alias, link_units
from vetromar.mcp_server import server as srv
from vetromar.schema import ClaimPayload, ExcerptEvidence, PersonRef, UnitDraft

WHEN = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _seed(store):
    episode, (room_unit,) = ingest_room(
        store, [make_billing_unit()], title="Architecture review", occurred_at=WHEN
    )
    ep = add_source_episode(
        store, title="JIRA BILL-142", source_kind="document",
        raw="Ticket: carve invoicing service out of monolith",
        raw_ref="https://jira/BILL-142",
    )
    ticket = add_draft(store, ep.id, UnitDraft(
        content="Carve invoicing service out of monolith",
        reasoning="Spawned by the architecture review",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text="carve invoicing service out of monolith",
                                  author=PersonRef(ref="priya.k"))],
    ))
    link_units(store, room_unit.id, ticket.id, kind="spawned")
    return room_unit, ticket


# The ONLY sanctioned write tools — the generic ingestion surface (engine v2,
# a deliberate scope decision). Anything else write-shaped is a scope breach.
WRITE_TOOL_ALLOWLIST = {"ingest_episode", "add_units", "link_units", "supersede_unit"}


def test_write_surface_is_exactly_the_allowlist():
    """Write tools are deliberate and enumerated; every other tool must stay
    read-shaped. This canary is the fence."""
    write_words = ("add", "create", "delete", "update", "write", "link", "supersede", "ingest", "push")
    tool_names = {
        name for name, obj in vars(srv).items()
        if callable(obj) and getattr(obj, "__module__", "") == srv.__name__
        and not name.startswith("_") and name != "main"
    }
    assert tool_names, "no tools found"
    write_shaped = {n for n in tool_names if any(w in n for w in write_words)}
    assert write_shaped == WRITE_TOOL_ALLOWLIST


def test_get_unit_carries_provenance_and_edges(store, monkeypatch):
    monkeypatch.setattr(srv, "_get_store", lambda: store)
    room_unit, ticket = _seed(store)

    payload = srv.get_unit(room_unit.id)
    # the fusion must be visible to the querying agent
    assert payload["episode"]["title"] == "Architecture review"
    assert payload["episode"]["source_kind"] == "meeting"
    assert "raw" not in payload["episode"]  # raw is fetched explicitly, never embedded
    assert payload["edges"][0]["to_id"] == ticket.id
    assert payload["edges"][0]["kind"] == "spawned"
    assert payload["unit"]["type"] == "decision"
    assert payload["unit"]["provenance"]["episode_id"] == payload["episode"]["id"]


def test_search_and_episode_tools(store, monkeypatch):
    monkeypatch.setattr(srv, "_get_store", lambda: store)
    room_unit, ticket = _seed(store)

    # ranked text search + post-filter: both units mention 'monolith'; method
    # narrows to the captured one.
    hits = srv.search_units(text="monolith", method="captured")
    assert [h["unit"]["id"] for h in hits] == [room_unit.id]

    # no text -> filtered listing
    listing = srv.search_units(type="claim")
    assert [h["unit"]["id"] for h in listing] == [ticket.id]

    eps = srv.list_episodes()
    assert {e["source_kind"] for e in eps} == {"meeting", "document"}
    assert all("raw" not in e for e in eps)

    ep = srv.get_episode(ticket.provenance.episode_id)
    assert ep["episode"]["raw_ref"] == "https://jira/BILL-142"
    assert "raw" not in ep["episode"]
    assert [u["id"] for u in ep["units"]] == [ticket.id]

    with_raw = srv.get_episode(ticket.provenance.episode_id, include_raw=True)
    assert "invoicing" in with_raw["episode"]["raw"]


def test_entity_tools(store, monkeypatch):
    monkeypatch.setattr(srv, "_get_store", lambda: store)
    room_unit, _ = _seed(store)
    priya = create_entity(store, "Priya")
    link_alias(store, priya.id, "SPEAKER_02")

    entities = srv.list_entities()
    assert [e["name"] for e in entities] == ["Priya"]
    assert srv.list_entities(type="project") == []

    found = srv.units_by_entity(priya.id)
    assert [h["unit"]["id"] for h in found] == [room_unit.id]
