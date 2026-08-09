"""Desktop UI HTTP API — wiring only. The engine (pipeline, extraction, store)
is tested elsewhere; here we prove the routes delegate to it correctly, run the
long work as polled jobs, and surface setup/backend state."""

import re
import sys
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import vetromar.capture.audio as audio_mod
import vetromar.capture.pipeline as pipeline_mod
from vetromar import operations
from vetromar.schema import (
    DecisionPayload,
    Episode,
    PersonRef,
    Provenance,
    QuoteEvidence,
    Status,
    Unit,
)
from vetromar.ui_server.app import create_app


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    import vetromar.config as config_mod

    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # Isolate from the real 0600 credentials file (~/.vetromar/credentials),
    # which exists once the user has run setup — otherwise the no-key test sees it.
    monkeypatch.setattr(config_mod, "CREDENTIALS_PATH", tmp_path / "credentials")
    # Same for the Deepgram key — a real credentials-deepgram file would flip
    # transcription to cloud in these tests.
    monkeypatch.setattr(config_mod, "DEEPGRAM_CREDENTIALS_PATH", tmp_path / "credentials-deepgram")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("VETROMAR_TRANSCRIBE", raising=False)
    return tmp_path


@pytest.fixture
def client(isolated_env):
    return TestClient(create_app())


def _fake_pipeline(audio_path, **kwargs):
    ep = Episode(
        source_kind="meeting", title="t", occurred_at=datetime.now(timezone.utc),
        raw='{"segments": []}',
    )
    unit = Unit(
        content="Ship it",
        reasoning="because",
        payload=DecisionPayload(status=Status.DECIDED),
        evidence=[
            QuoteEvidence(text="ship it", speaker=PersonRef(ref="SPEAKER_00"), start_ms=0, end_ms=1)
        ],
        provenance=Provenance(method="captured", episode_id=ep.id),
    )
    return ep, [unit], "# rendered"


def _wait(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {last}")


def test_health_ready_in_api_mode_with_key(client):
    from tests.conftest import warm_fake_transcription_caches

    # Transcription resolves local here (no Deepgram key / sign-in), so ready
    # also requires the local transcription weights (M17: explicit downloads).
    warm_fake_transcription_caches()
    r = client.get("/api/health").json()
    assert r["backend"] == "api"
    assert r["ready"] is True
    assert any(c["label"] == "Anthropic API key present" and c["ok"] for c in r["checks"])
    # M17 surface: the Settings screen reads these from health.
    assert set(r["local_models"]) == {"extraction", "transcription", "embedding"}


def test_health_not_ready_without_key(isolated_env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = TestClient(create_app()).get("/api/health").json()
    assert r["ready"] is False


def test_setup_cloud_selects_api_backend(client, monkeypatch):
    # No key in the body — access comes from the configured provider.
    seen = {}
    monkeypatch.setattr(
        operations, "select_api_backend", lambda config: seen.__setitem__("called", True)
    )
    r = client.post("/api/setup/cloud")
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "api"
    assert seen["called"]


def test_setup_cloud_without_access_is_400(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post("/api/setup/cloud")
    assert r.status_code == 400
    assert "AI provider" in r.json()["detail"]


def test_website_open_joins_configured_base(client, monkeypatch):
    import webbrowser

    seen = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: seen.setdefault("url", url) or True)
    r = client.post("/api/website/open", json={"path": "create-workspace.html"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["opened"] is True
    assert body["url"].endswith("/create-workspace.html")
    assert seen["url"] == body["url"]


def test_workspace_token_alone_is_not_ai_access(isolated_env, monkeypatch):
    # Post-pivot a workspace session is sync-only — health must not report
    # AI access from it.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VETROMAR_CLOUD_TOKEN", "tok_ws")
    r = TestClient(create_app()).get("/api/health").json()
    assert r["ready"] is False
    check = next(c for c in r["checks"] if c["label"] == "AI access")
    assert check["ok"] is False


def test_setup_local_select_writes_config_without_jobs(client, isolated_env):
    r = client.post("/api/setup/local-select")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "backend": "local"}
    text = (isolated_env / "config.toml").read_text()
    assert 'backend = "local"' in text
    assert 'transcribe = "local"' in text


def test_models_download_runs_as_job_and_dedupes(client, monkeypatch):
    gate = threading.Event()
    calls = []

    def fake_download(config, progress=None):
        if progress:
            progress("Downloading speech model", None, None)
        calls.append("download")
        assert gate.wait(5), "test gate never opened"

    monkeypatch.setattr(operations, "download_local_models", fake_download)
    first = client.post("/api/models/download").json()
    assert first["already_running"] is False
    # A second click while running attaches to the SAME job.
    second = client.post("/api/models/download").json()
    assert second["job_id"] == first["job_id"]
    assert second["already_running"] is True
    gate.set()
    job = _wait(client, first["job_id"])
    assert job["status"] == "done"
    assert job["kind"] == "download-models"
    assert calls == ["download"]
    assert any("Downloading speech model" in line for line in job["progress"])


def test_setup_deepgram_route_validates_and_saves(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        operations, "validate_and_save_deepgram_key", lambda key: seen.__setitem__("key", key)
    )
    r = client.post("/api/setup/deepgram", json={"api_key": "dg-x"})
    assert r.status_code == 200, r.text
    assert seen["key"] == "dg-x"
    # Rejected keys surface inline as 400s.
    def reject(key):
        raise operations.InvalidApiKey("That key was rejected by Deepgram.")

    monkeypatch.setattr(operations, "validate_and_save_deepgram_key", reject)
    r = client.post("/api/setup/deepgram", json={"api_key": "bad"})
    assert r.status_code == 400
    assert "rejected" in r.json()["detail"]
    # Missing/blank keys never reach validation.
    assert client.post("/api/setup/deepgram", json={}).status_code == 422
    assert client.post("/api/setup/deepgram", json={"api_key": "  "}).status_code == 400


def test_provider_settings_roundtrip(client, monkeypatch):
    r = client.get("/api/settings/provider").json()
    assert r["provider"] == "anthropic"
    assert r["has_anthropic_key"] is True  # fixture env key
    assert "has_deepgram_key" in r and "openai_base_url" in r

    seen = {}
    monkeypatch.setattr(
        operations,
        "configure_provider",
        lambda provider, **kw: seen.update({"provider": provider, **kw}),
    )
    r = client.post(
        "/api/setup/provider",
        json={"provider": "openai", "base_url": "http://localhost:11434/v1", "model": "qwen3.5:9b"},
    )
    assert r.status_code == 200, r.text
    assert seen == {
        "provider": "openai",
        "api_key": None,
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3.5:9b",
    }


def test_provider_setup_configerror_is_400(client):
    # A missing base URL is caught before any network/persistence.
    r = client.post("/api/setup/provider", json={"provider": "openai"})
    assert r.status_code == 400
    assert "base URL" in r.json()["detail"]
    r = client.post("/api/setup/provider", json={"provider": "nope"})
    assert r.status_code == 400


def test_health_reports_transcription_mode(client):
    r = client.get("/api/health").json()
    assert r["transcription"] == "local"  # no Deepgram key in the isolated env
    assert any(c["label"] == "transcription mode" for c in r["checks"])




def test_capture_runs_pipeline_as_job(client, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "run_pipeline", _fake_pipeline)

    r = client.post(
        "/api/capture",
        data={"title": "Billing sync"},
        files={"file": ("meeting.wav", b"RIFFfake", "audio/wav")},
    )
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["units"][0]["content"] == "Ship it"
    assert job["result"]["markdown"] == "# rendered"
    assert "raw" not in job["result"]["episode"]  # transcript blob stays server-side


_DEFAULT_TITLE = r"Meeting \d{4}-\d{2}-\d{2} \d{2}:\d{2}"


def test_capture_without_title_gets_dated_default(client, monkeypatch):
    received = {}

    def fake(audio_path, **kwargs):
        received.update(kwargs)
        return _fake_pipeline(audio_path, **kwargs)

    monkeypatch.setattr(pipeline_mod, "run_pipeline", fake)
    # No title field at all, then an all-whitespace one — both get the default.
    for data in ({}, {"title": "   "}):
        r = client.post(
            "/api/capture", data=data, files={"file": ("m.wav", b"x", "audio/wav")}
        )
        assert r.status_code == 200, r.text
        assert _wait(client, r.json()["job_id"])["status"] == "done"
        assert re.fullmatch(_DEFAULT_TITLE, received["title"]), received["title"]


def test_record_without_title_gets_dated_default(client, monkeypatch):
    received = {}

    def fake(audio_path, **kwargs):
        received.update(kwargs)
        return _fake_pipeline(audio_path, **kwargs)

    monkeypatch.setattr(audio_mod, "record_mic", lambda out_path, **kw: out_path)
    monkeypatch.setattr(pipeline_mod, "run_pipeline", fake)
    r = client.post("/api/record/start", json={})
    assert r.status_code == 200, r.text
    assert _wait(client, r.json()["job_id"])["status"] == "done"
    assert re.fullmatch(_DEFAULT_TITLE, received["title"]), received["title"]


def test_capture_api_mode_does_not_touch_runtime(client, monkeypatch):
    import vetromar.runtime as runtime_mod

    monkeypatch.setattr(pipeline_mod, "run_pipeline", _fake_pipeline)
    monkeypatch.setattr(
        runtime_mod, "ensure_server", lambda c: (_ for _ in ()).throw(AssertionError("no runtime in api mode"))
    )
    r = client.post(
        "/api/capture", data={"title": "M"}, files={"file": ("m.wav", b"x", "audio/wav")}
    )
    assert _wait(client, r.json()["job_id"])["status"] == "done"


def test_pipeline_failure_becomes_generic_job_error(client, monkeypatch):
    # An unexpected crash must NOT leak its exception text to the UI — the
    # user sees the generic VM-100 message, the traceback goes to the log.
    def boom(audio_path, **kwargs):
        raise RuntimeError("sqlite3.OperationalError: database is locked")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", boom)
    r = client.post(
        "/api/capture", data={"title": "M"}, files={"file": ("m.wav", b"x", "audio/wav")}
    )
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "error"
    assert "database is locked" not in job["error"]
    assert "VM-100" in job["error"]
    assert job["error_code"] == "VM-100"


def test_gate_failure_becomes_vm200_job_error(client, monkeypatch):
    from vetromar.extraction.validate import EvidenceMismatchError

    def boom(audio_path, **kwargs):
        raise EvidenceMismatchError(["we weren't sure about that decision"])

    monkeypatch.setattr(pipeline_mod, "run_pipeline", boom)
    r = client.post(
        "/api/capture", data={"title": "M"}, files={"file": ("m.wav", b"x", "audio/wav")}
    )
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "error"
    assert "weren't sure" not in job["error"]  # no transcript fragments in the UI
    assert job["error_code"] == "VM-200"


def test_record_start_stop_roundtrip(client, monkeypatch):
    def fake_record_mic(out_path, duration_s=None, samplerate=16000, stop_event=None):
        # Behave like the real mic: block until the API signals stop.
        assert stop_event is not None
        stop_event.wait(timeout=5)
        return out_path

    monkeypatch.setattr(audio_mod, "record_mic", fake_record_mic)
    monkeypatch.setattr(pipeline_mod, "run_pipeline", _fake_pipeline)

    started = client.post("/api/record/start", json={"title": "Standup"}).json()
    job_id = started["job_id"]

    # Wait until the worker is actually recording, then stop it.
    for _ in range(100):
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "recording":
            break
        time.sleep(0.02)
    stop = client.post("/api/record/stop", json={"job_id": job_id})
    assert stop.status_code == 200

    job = _wait(client, job_id)
    assert job["status"] == "done"
    assert job["result"]["units"][0]["content"] == "Ship it"


def test_record_stop_unknown_job_404(client):
    assert client.post("/api/record/stop", json={"job_id": "nope"}).status_code == 404


def test_get_unknown_job_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


# -- store browsing routes (read-only window over the knowledge store) --------


@pytest.fixture
def seeded(isolated_env):
    """A store at VETROMAR_DB with a meeting decision, a doc claim it spawned,
    a superseding claim, an entity, and the edges between them — enough graph
    to exercise every read route. Returns the ids."""
    from tests.conftest import make_billing_unit
    from vetromar.ingest import ingest_room
    from vetromar.ingest.manual import (
        add_draft,
        add_source_episode,
        create_entity,
        link_alias,
        link_units,
    )
    from vetromar.schema import ClaimPayload, ExcerptEvidence, PersonRef, UnitDraft
    from vetromar.store import Store

    store = Store(isolated_env / "store.db")
    try:
        episode, (room_unit,) = ingest_room(
            store, [make_billing_unit()],
            title="Architecture review",
            occurred_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        )
        ep = add_source_episode(
            store, title="JIRA BILL-142", source_kind="document",
            raw="Ticket: carve invoicing service out of monolith. Update: split into two tickets.",
        )
        ticket = add_draft(store, ep.id, UnitDraft(
            content="Carve invoicing service out of monolith",
            payload=ClaimPayload(),
            evidence=[ExcerptEvidence(text="carve invoicing service out of monolith")],
        ))
        replacement = add_draft(store, ep.id, UnitDraft(
            content="Invoicing carve-out split into two tickets",
            payload=ClaimPayload(),
            evidence=[ExcerptEvidence(text="split into two tickets")],
        ))
        link_units(store, room_unit.id, ticket.id, kind="spawned")
        store.supersede(ticket.id, replacement.id)
        store.add_edge(replacement.id, ticket.id, kind="supersedes", method="manual")
        priya = create_entity(store, "Priya")
        link_alias(store, priya.id, "SPEAKER_02")
        store.add_edge(room_unit.id, priya.id, kind="mentions", method="manual")
        return {
            "room_unit": room_unit.id,
            "ticket": ticket.id,
            "replacement": replacement.id,
            "episode": episode.id,
            "doc_episode": ep.id,
            "priya": priya.id,
        }
    finally:
        store.close()


def test_store_search_text_and_filters(client, seeded):
    # ranked text search + method filter narrows to the captured decision
    hits = client.get("/api/store/search", params={"text": "monolith", "method": "captured"}).json()
    assert [h["unit"]["id"] for h in hits] == [seeded["room_unit"]]
    assert "labels" in hits[0]  # the UI extra the MCP shape doesn't carry

    # no text -> filtered listing; current_only excludes the superseded ticket
    listing = client.get("/api/store/search", params={"type": "claim", "current_only": True}).json()
    assert [h["unit"]["id"] for h in listing] == [seeded["replacement"]]


def test_store_unit_detail_labels_and_404(client, seeded):
    payload = client.get(f"/api/store/units/{seeded['room_unit']}").json()
    assert payload["episode"]["title"] == "Architecture review"
    assert "raw" not in payload["episode"]
    labels = payload["labels"]
    assert labels[seeded["ticket"]]["node"] == "unit"
    assert labels[seeded["priya"]] == {"label": "Priya", "node": "entity"}

    # the superseded unit carries its closed validity + the supersedes edge
    old = client.get(f"/api/store/units/{seeded['ticket']}").json()
    assert old["unit"]["valid_to"] is not None
    kinds = {e["kind"] for e in old["edges"]}
    assert "supersedes" in kinds

    assert client.get("/api/store/units/unit_nope").status_code == 404


def test_store_episodes_and_raw_toggle(client, seeded):
    eps = client.get("/api/store/episodes").json()
    assert {e["source_kind"] for e in eps} == {"meeting", "document"}
    assert all("raw" not in e for e in eps)

    detail = client.get(f"/api/store/episodes/{seeded['doc_episode']}").json()
    assert "raw" not in detail["episode"]
    assert {u["id"] for u in detail["units"]} == {seeded["ticket"], seeded["replacement"]}

    with_raw = client.get(
        f"/api/store/episodes/{seeded['doc_episode']}", params={"include_raw": True}
    ).json()
    assert "invoicing" in with_raw["episode"]["raw"]

    assert client.get("/api/store/episodes/ep_nope").status_code == 404


def test_store_episode_rename(client, seeded):
    url = f"/api/store/episodes/{seeded['doc_episode']}/rename"
    r = client.post(url, json={"title": "  BILL-142 carve-out  "})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "BILL-142 carve-out"
    assert "raw" not in r.json()

    eps = {e["id"]: e["title"] for e in client.get("/api/store/episodes").json()}
    assert eps[seeded["doc_episode"]] == "BILL-142 carve-out"

    assert client.post(url, json={"title": "   "}).status_code == 400
    assert client.post(url, json={}).status_code == 422
    assert (
        client.post("/api/store/episodes/ep_nope/rename", json={"title": "x"}).status_code
        == 404
    )


def test_store_entities_and_their_units(client, seeded):
    entities = client.get("/api/store/entities").json()
    assert [e["name"] for e in entities] == ["Priya"]
    assert client.get("/api/store/entities", params={"type": "project"}).json() == []

    units = client.get(f"/api/store/entities/{seeded['priya']}/units").json()
    assert [u["unit"]["id"] for u in units] == [seeded["room_unit"]]

    assert client.get("/api/store/entities/ent_nope/units").status_code == 404


def test_store_graph_payload(client, seeded):
    g = client.get("/api/store/graph").json()

    assert [e["name"] for e in g["entities"]] == ["Priya"]
    units = {u["id"]: u for u in g["units"]}
    assert set(units) == {seeded["room_unit"], seeded["ticket"], seeded["replacement"]}
    assert units[seeded["ticket"]]["superseded"] is True
    assert units[seeded["room_unit"]]["superseded"] is False
    # every unit carries evidence snippets and resolvable provenance
    episode_ids = {e["id"] for e in g["episodes"]}
    for u in units.values():
        assert u["evidence"], u["id"]
        assert all(ev["kind"] and ev["text"] for ev in u["evidence"])
        assert u["episode_id"] in episode_ids
    # the stored edges are all present (spawned, supersedes, mentions)
    kinds = {e["kind"] for e in g["edges"]}
    assert {"spawned", "supersedes", "mentions"} <= kinds


# -- sources routes (the M10 connect/sync flow surfaced in the desktop UI) ----


@pytest.fixture
def sources_env(isolated_env, monkeypatch):
    """Point the sources registry + token storage at the tmp dir so the routes
    never touch the real ~/.vetromar/sources.toml or tokens."""
    import vetromar.sources.auth as auth_mod
    import vetromar.sources.registry as registry_mod

    monkeypatch.setattr(registry_mod, "VETROMAR_HOME", isolated_env)
    monkeypatch.setattr(auth_mod, "VETROMAR_HOME", isolated_env)
    return isolated_env


def test_sources_catalog_marks_connected(client, sources_env):
    from vetromar.sources.registry import SourceConfig, upsert_source

    entries = client.get("/api/sources/catalog").json()
    assert len(entries) >= 6
    assert all(e["connected"] is False for e in entries)

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    entries = {e["name"]: e for e in client.get("/api/sources/catalog").json()}
    assert entries["notion"]["connected"] is True
    assert entries["linear"]["connected"] is False


def test_sources_connect_catalog_runs_consent_then_persists(client, sources_env, monkeypatch):
    import vetromar.sources.client as sources_client

    monkeypatch.setattr(sources_client, "test_source", lambda source, cancel_event=None: ["search", "fetch"])

    r = client.post("/api/sources/connect", json={"name": "notion"})
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"] == {"name": "notion", "tools": ["search", "fetch"]}

    listed = client.get("/api/sources").json()
    assert [(s["name"], s["transport"], s["source_kind"]) for s in listed] == [
        ("notion", "http", "document")
    ]


def test_sources_connect_custom_url(client, sources_env, monkeypatch):
    import vetromar.sources.client as sources_client

    monkeypatch.setattr(sources_client, "test_source", lambda source, cancel_event=None: ["list_pages"])

    r = client.post(
        "/api/sources/connect",
        json={"name": "wiki", "url": "https://wiki.example/mcp", "source_kind": "chat"},
    )
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done", job
    (s,) = client.get("/api/sources").json()
    assert (s["name"], s["source_kind"], s["where"]) == ("wiki", "chat", "https://wiki.example/mcp")


def test_sources_connect_unknown_catalog_name_400(client, sources_env):
    r = client.post("/api/sources/connect", json={"name": "not-a-thing"})
    assert r.status_code == 400
    assert "catalog" in r.json()["detail"]


def test_sources_catalog_carries_credential_fields(client, sources_env):
    entries = {e["name"]: e for e in client.get("/api/sources/catalog").json()}
    assert entries["slack"]["needs_client_registration"] is True
    assert entries["slack"]["setup_url"].startswith("https://api.slack.com")
    # The URL customers configure on their app: the HTTPS relay page.
    assert entries["slack"]["redirect_uri"].startswith("https://")
    assert entries["slack"]["redirect_uri"].endswith("/oauth-callback.html")
    assert entries["notion"]["needs_client_registration"] is False


def test_sources_connect_no_dcr_without_credentials_400(client, sources_env):
    r = client.post("/api/sources/connect", json={"name": "slack"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "client ID" in detail and "api.slack.com" in detail


def test_sources_connect_no_dcr_with_credentials_seeds_then_connects(
    client, sources_env, monkeypatch
):
    import vetromar.sources.client as sources_client
    from vetromar.sources import auth

    monkeypatch.setattr(
        sources_client, "test_source", lambda source, cancel_event=None: ["list_messages"]
    )
    r = client.post(
        "/api/sources/connect",
        json={"name": "slack", "client_id": "app-id", "client_secret": "app-secret"},
    )
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done", job
    assert auth.has_client_info("slack")
    (s,) = client.get("/api/sources").json()
    assert (s["name"], s["source_kind"]) == ("slack", "chat")


def test_sources_setup_page_route(client, sources_env, monkeypatch):
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    r = client.post("/api/sources/slack/setup-page")
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://api.slack.com")
    assert opened == [r.json()["url"]]
    # one-click entries have no setup page; neither do unknown names
    assert client.post("/api/sources/notion/setup-page").status_code == 404
    assert client.post("/api/sources/nope/setup-page").status_code == 404


def test_sources_connect_failure_is_job_error_and_not_saved(client, sources_env, monkeypatch):
    import vetromar.sources.client as sources_client
    from vetromar.errors import ConfigError

    def boom(source, cancel_event=None):
        raise ConfigError("Could not reach the server.", hint="Check the URL.")

    monkeypatch.setattr(sources_client, "test_source", boom)
    r = client.post("/api/sources/connect", json={"name": "notion"})
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "error"
    assert "Could not reach" in job["error"]
    # upsert only happens after a successful consent — nothing was persisted
    assert client.get("/api/sources").json() == []


def test_sources_test_and_unknown_404(client, sources_env, monkeypatch):
    import vetromar.sources.client as sources_client
    from vetromar.sources.registry import SourceConfig, upsert_source

    assert client.post("/api/sources/nope/test").status_code == 404

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    monkeypatch.setattr(sources_client, "test_source", lambda source, cancel_event=None: ["search"])
    job = _wait(client, client.post("/api/sources/notion/test").json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["tools"] == ["search"]


def test_sources_remove_deletes_registry_and_tokens(client, sources_env):
    from vetromar.sources.registry import SourceConfig, upsert_source

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    token_file = sources_env / "tokens" / "notion.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("{}")

    assert client.delete("/api/sources/notion").status_code == 200
    assert client.get("/api/sources").json() == []
    assert not token_file.exists()

    assert client.delete("/api/sources/notion").status_code == 404


def test_sources_sync_job_reports_and_taps_agent_log(client, sources_env, monkeypatch):
    import logging

    import vetromar.sources.sync as sync_mod
    from vetromar.sources.registry import SourceConfig, upsert_source
    from vetromar.sources.sync import SyncReport

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    seen = {}

    def fake_sync(store, source, config, *, full, dry_run, extract):
        seen.update(full=full, dry_run=dry_run, extract=extract)
        # the route taps this logger into job.progress for the duration
        logging.getLogger("vetromar.sources.sync").info("sync notion: calling fetch_pages")
        return SyncReport(
            source=source.name, created=["p1", "p2"], duplicates=["p0"],
            units=3, cursor="2026-07-19", dry_run=dry_run,
        )

    monkeypatch.setattr(sync_mod, "sync_source", fake_sync)
    r = client.post("/api/sources/notion/sync", json={"dry_run": True})
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done", job
    assert seen == {"full": False, "dry_run": True, "extract": True}
    assert job["result"]["created"] == ["p1", "p2"]
    assert job["result"]["units"] == 3
    assert job["result"]["dry_run"] is True
    assert any("calling fetch_pages" in line for line in job["progress"])
    # the log tap is removed once the job finishes
    assert not logging.getLogger("vetromar.sources.sync").handlers


def test_sources_connect_cancel_aborts_consent_wait(client, sources_env, monkeypatch):
    import vetromar.sources.client as sources_client
    from vetromar.errors import ConfigError

    def fake_test_source(source, cancel_event=None):
        # Behave like a pending OAuth consent: block until cancelled.
        assert cancel_event is not None
        if not cancel_event.wait(timeout=5):
            raise AssertionError("cancel never arrived")
        raise ConfigError("Authorization cancelled.", hint="Run connect again to retry.")

    monkeypatch.setattr(sources_client, "test_source", fake_test_source)
    r = client.post("/api/sources/connect", json={"name": "notion"})
    job_id = r.json()["job_id"]

    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    job = _wait(client, job_id)
    assert job["status"] == "error"
    assert "cancelled" in job["error"].lower()
    # nothing persisted — the retry starts clean
    assert client.get("/api/sources").json() == []


def test_cancel_only_applies_to_connect_and_test_jobs(client, sources_env, monkeypatch):
    import vetromar.sources.sync as sync_mod
    from vetromar.sources.registry import SourceConfig, upsert_source
    from vetromar.sources.sync import SyncReport

    assert client.post("/api/jobs/nope/cancel").status_code == 404

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    monkeypatch.setattr(
        sync_mod,
        "sync_source",
        lambda store, source, config, **kw: SyncReport(source=source.name),
    )
    job_id = client.post("/api/sources/notion/sync", json={}).json()["job_id"]
    _wait(client, job_id)
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_sources_sync_config_error_is_job_error(client, sources_env, monkeypatch):
    import vetromar.sources.sync as sync_mod
    from vetromar.errors import ConfigError
    from vetromar.sources.registry import SourceConfig, upsert_source

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))

    def boom(store, source, config, **kwargs):
        raise ConfigError("Source sync runs on the API backend only.")

    monkeypatch.setattr(sync_mod, "sync_source", boom)
    job = _wait(client, client.post("/api/sources/notion/sync", json={}).json()["job_id"])
    assert job["status"] == "error"
    assert "API backend" in job["error"]

    assert client.post("/api/sources/nope/sync", json={}).status_code == 404


def test_store_current_groups_by_entity(client, seeded):
    state = client.get("/api/store/current").json()
    buckets = {b["entity"]["name"]: [u["id"] for u in b["units"]] for b in state["entities"]}
    assert buckets == {"Priya": [seeded["room_unit"]]}
    unattributed = [u["id"] for u in state["unattributed"]]
    assert seeded["replacement"] in unattributed
    assert seeded["ticket"] not in unattributed  # superseded -> not current

    focused = client.get("/api/store/current", params={"entity_id": seeded["priya"]}).json()
    assert focused["entities"][0]["units"][0]["id"] == seeded["room_unit"]


# -- M13: sync overlap guard, job listing, auto-sync settings -----------------


def test_sync_overlap_attaches_to_running_job(client, sources_env, monkeypatch):
    import vetromar.sources.sync as sync_mod
    from vetromar.sources.registry import SourceConfig, upsert_source
    from vetromar.sources.sync import SyncReport

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    release = threading.Event()

    def slow_sync(store, source, config, **kw):
        assert release.wait(timeout=5)
        return SyncReport(source=source.name)

    monkeypatch.setattr(sync_mod, "sync_source", slow_sync)
    first = client.post("/api/sources/notion/sync", json={}).json()
    assert first["already_running"] is False
    # a second sync for the same source attaches to the in-flight job
    second = client.post("/api/sources/notion/sync", json={}).json()
    assert second["job_id"] == first["job_id"]
    assert second["already_running"] is True

    active = client.get("/api/jobs", params={"kind": "sync", "active": True}).json()
    assert [j["id"] for j in active] == [first["job_id"]]
    assert active[0]["meta"] == {"source": "notion", "initiator": "manual"}

    release.set()
    _wait(client, first["job_id"])
    assert client.get("/api/jobs", params={"kind": "sync", "active": True}).json() == []
    # after completion a new sync starts a NEW job
    third = client.post("/api/sources/notion/sync", json={}).json()
    assert third["already_running"] is False
    assert third["job_id"] != first["job_id"]
    release.set()
    _wait(client, third["job_id"])


def test_sync_result_reports_incomplete(client, sources_env, monkeypatch):
    import vetromar.sources.sync as sync_mod
    from vetromar.sources.registry import SourceConfig, upsert_source
    from vetromar.sources.sync import SyncReport

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    monkeypatch.setattr(
        sync_mod,
        "sync_source",
        lambda store, source, config, **kw: SyncReport(
            source=source.name, created=["p1"], incomplete=True
        ),
    )
    job = _wait(client, client.post("/api/sources/notion/sync", json={}).json()["job_id"])
    assert job["result"]["incomplete"] is True


def test_sources_list_carries_last_synced_at(client, sources_env):
    from vetromar.config import load_config
    from vetromar.sources.registry import SourceConfig, upsert_source
    from vetromar.store import Store

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    (entry,) = client.get("/api/sources").json()
    assert entry["last_synced_at"] is None

    config = load_config()
    config.ensure_dirs()
    store = Store(config.db_path)
    store.set_sync_state("notion", "{}")
    store.close()
    (entry,) = client.get("/api/sources").json()
    assert entry["last_synced_at"] is not None


def test_auto_sync_settings_roundtrip_and_validation(client, monkeypatch):
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_INTERVAL_MINUTES", raising=False)

    assert client.get("/api/settings/auto-sync").json() == {
        "enabled": False,
        "interval_minutes": 60,
    }
    r = client.post("/api/settings/auto-sync", json={"enabled": True, "interval_minutes": 4})
    assert r.status_code == 400
    r = client.post("/api/settings/auto-sync", json={"enabled": True, "interval_minutes": 15})
    assert r.status_code == 200
    assert client.get("/api/settings/auto-sync").json() == {
        "enabled": True,
        "interval_minutes": 15,
    }
    from vetromar.config import load_config

    config = load_config()
    assert config.auto_sync_enabled is True
    assert config.auto_sync_interval_minutes == 15


# -- onboarding (first-run tour + getting-started checklist flags/detection) --


@pytest.fixture
def onboarding_env(sources_env, monkeypatch):
    monkeypatch.delenv("VETROMAR_ONBOARDING_TOUR_DONE", raising=False)
    monkeypatch.delenv("VETROMAR_ONBOARDING_CHECKLIST_DISMISSED", raising=False)
    return sources_env


def test_onboarding_status_defaults(client, onboarding_env):
    assert client.get("/api/onboarding").json() == {
        "tour_done": False,
        "checklist_dismissed": False,
        "source_synced": False,
        "meeting_captured": False,
    }


def test_onboarding_flags_roundtrip(client, onboarding_env):
    r = client.post("/api/onboarding", json={"tour_done": True}).json()
    assert r["tour_done"] is True
    assert r["checklist_dismissed"] is False
    r = client.post("/api/onboarding", json={"checklist_dismissed": True}).json()
    # Partial update: the other flag survives the second write.
    assert r["tour_done"] is True
    assert r["checklist_dismissed"] is True
    from vetromar.config import load_config

    config = load_config()
    assert config.onboarding_tour_done is True
    assert config.onboarding_checklist_dismissed is True


def test_onboarding_env_overrides_saved_config(client, onboarding_env, monkeypatch):
    client.post("/api/onboarding", json={"tour_done": True})
    monkeypatch.setenv("VETROMAR_ONBOARDING_TOUR_DONE", "false")
    assert client.get("/api/onboarding").json()["tour_done"] is False


def test_onboarding_detects_captured_meeting(client, onboarding_env):
    from vetromar.config import load_config
    from vetromar.store import Store

    config = load_config()
    config.ensure_dirs()
    store = Store(config.db_path)
    # A synced-source episode is NOT a captured meeting.
    store.add_episode(
        Episode(
            source_kind="document", title="doc", occurred_at=datetime.now(timezone.utc),
            raw="notes",
        )
    )
    assert client.get("/api/onboarding").json()["meeting_captured"] is False
    store.add_episode(
        Episode(
            source_kind="meeting", title="standup", occurred_at=datetime.now(timezone.utc),
            raw='{"segments": []}',
        )
    )
    store.close()
    assert client.get("/api/onboarding").json()["meeting_captured"] is True


def test_onboarding_detects_synced_source(client, onboarding_env):
    from vetromar.config import load_config
    from vetromar.sources.registry import SourceConfig, upsert_source
    from vetromar.store import Store

    upsert_source(SourceConfig(name="notion", url="https://mcp.notion.com/mcp"))
    # Connected but never synced: stays false.
    assert client.get("/api/onboarding").json()["source_synced"] is False

    config = load_config()
    config.ensure_dirs()
    store = Store(config.db_path)
    store.set_sync_state("notion", "{}")
    store.close()
    assert client.get("/api/onboarding").json()["source_synced"] is True


# -- MCP shim (the agent-facing entry point on customer machines) -------------


def test_mcp_info_and_shim_install(client, isolated_env, monkeypatch):
    from vetromar import operations

    monkeypatch.setattr(operations, "VETROMAR_HOME", isolated_env)
    info = client.get("/api/mcp").json()
    assert info["shim_path"] == str(isolated_env / "bin" / "vetromar-mcp")
    assert info["installed"] is False
    # The paste prompt carries the shim path + server name — the frontend
    # never composes this text.
    assert info["shim_path"] in info["prompt"]
    assert '"vetromar"' in info["prompt"]

    path = operations.install_mcp_shim()
    text = path.read_text()
    assert text.startswith("#!/bin/sh")
    assert ' serve "$@"' in text
    assert path.stat().st_mode & 0o111  # executable
    assert client.get("/api/mcp").json()["installed"] is True


def test_mcp_shim_frozen_points_at_sidecar_binary(isolated_env, monkeypatch):
    from vetromar import operations

    monkeypatch.setattr(operations, "VETROMAR_HOME", isolated_env)
    sidecar = "/Applications/Vetromar.app/Contents/Resources/sidecar/vetromar-sidecar/vetromar-sidecar"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", sidecar)
    path = operations.install_mcp_shim()
    assert f"exec {sidecar} serve" in path.read_text()


def test_mcp_shim_unresolvable_engine_returns_none(isolated_env, monkeypatch):
    import shutil as shutil_mod

    from vetromar import operations

    monkeypatch.setattr(operations, "VETROMAR_HOME", isolated_env)
    monkeypatch.setattr(sys, "executable", str(isolated_env / "nowhere" / "python"))
    monkeypatch.setattr(shutil_mod, "which", lambda _: None)
    assert operations.install_mcp_shim() is None
    assert not (isolated_env / "bin" / "vetromar-mcp").exists()
