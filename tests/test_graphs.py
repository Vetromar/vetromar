"""Multi-graph core (G1): the registry, per-graph route scoping, quick notes,
and the MCP graph selector. The engine underneath is store-parameterized and
tested elsewhere — here we prove graph ids resolve to the right stores and
nothing leaks between graphs."""

import pytest
from fastapi.testclient import TestClient

from vetromar import graphs
from vetromar.graphs import GraphError
from vetromar.ingest.notes import add_quick_note
from vetromar.ui_server.app import create_app


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return tmp_path


@pytest.fixture
def client(isolated_env):
    return TestClient(create_app())


# -- registry ------------------------------------------------------------------


def test_private_graph_is_synthesized_never_stored(isolated_env):
    listed = graphs.list_graphs()
    assert [g.id for g in listed] == ["private"]
    assert listed[0].kind == "private"
    assert not graphs.registry_path().exists()  # nothing written for private


def test_create_resolve_and_remove_graph(isolated_env):
    info = graphs.create_graph("Crew")
    assert info.name == "Crew" and info.kind == "shared"
    assert graphs.get_graph(info.id).name == "Crew"
    db = graphs.resolve_db_path(info.id)
    assert db == graphs.graph_dir(info.id) / "store.db"
    # private resolves to the legacy config path, untouched by the registry
    assert graphs.resolve_db_path(None) == graphs.resolve_db_path("private")

    graphs.remove_graph(info.id, delete_files=True)
    with pytest.raises(GraphError):
        graphs.get_graph(info.id)
    assert not graphs.graph_dir(info.id).exists()


def test_registry_guards(isolated_env):
    with pytest.raises(GraphError):
        graphs.create_graph("   ")
    with pytest.raises(GraphError):
        graphs.remove_graph("private")
    with pytest.raises(GraphError):
        graphs.resolve_db_path("g_nope")


def test_corrupt_registry_reads_as_empty(isolated_env):
    graphs.create_graph("Crew")
    graphs.registry_path().write_text("{not json")
    assert [g.id for g in graphs.list_graphs()] == ["private"]


# -- ui_server routes ------------------------------------------------------------


def test_graphs_api_create_list_remove(client):
    assert [g["id"] for g in client.get("/api/graphs").json()] == ["private"]

    created = client.post("/api/graphs", json={"name": "Crew"}).json()
    assert created["kind"] == "shared"
    ids = [g["id"] for g in client.get("/api/graphs").json()]
    assert ids == ["private", created["id"]]

    assert client.delete(f"/api/graphs/{created['id']}").json() == {"ok": True}
    assert client.post("/api/graphs", json={"name": " "}).status_code == 400
    assert client.delete("/api/graphs/private").status_code == 400


def test_note_lands_in_the_right_graph_only(client):
    g = client.post("/api/graphs", json={"name": "Crew"}).json()

    resp = client.post(f"/api/graphs/{g['id']}/note", json={"text": "the hinge should be brass"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["episode"]["source_kind"] == "note"
    assert body["unit"]["provenance"]["method"] == "concierge"

    # visible in the shared graph, absent from private — and vice versa
    shared = client.get("/api/store/search", params={"graph": g["id"]}).json()
    assert [u["unit"]["id"] for u in shared] == [body["unit"]["id"]]
    assert client.get("/api/store/search").json() == []
    episodes = client.get("/api/store/episodes", params={"graph": g["id"]}).json()
    assert [e["id"] for e in episodes] == [body["episode"]["id"]]
    assert client.get("/api/store/episodes").json() == []


def test_note_validation(client):
    g = client.post("/api/graphs", json={"name": "Crew"}).json()
    assert client.post(f"/api/graphs/{g['id']}/note", json={"text": "  "}).status_code == 400
    assert client.post("/api/graphs/g_nope/note", json={"text": "hi"}).status_code == 404


def test_unknown_graph_param_is_404(client):
    assert client.get("/api/store/search", params={"graph": "g_nope"}).status_code == 404


def test_note_title_derivation(store):
    episode, unit = add_quick_note(store, "first line here\nand a second line")
    assert episode.title == "first line here"
    assert unit.evidence[0].text == "first line here\nand a second line"
    episode2, _ = add_quick_note(store, "x" * 200, title="Custom")
    assert episode2.title == "Custom"


# -- MCP surface ----------------------------------------------------------------


def test_mcp_list_graphs_and_selector(isolated_env, monkeypatch):
    from vetromar.mcp_server import server as srv

    monkeypatch.setattr(srv, "_stores", {})
    info = graphs.create_graph("Crew")

    listed = srv.list_graphs()
    assert [g["id"] for g in listed] == ["private", info.id]

    ep = srv.ingest_episode(title="T", source_kind="note", raw="brass hinge", graph=info.id)
    assert [e["id"] for e in srv.list_episodes(graph=info.id)] == [ep["id"]]
    assert srv.list_episodes() == []  # default = private, untouched

    for s in srv._stores.values():
        s.close()
