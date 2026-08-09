"""Phase-B connect layer: the sources registry, the catalog, token storage,
and open_session over a real stdio transport against the fake chat server."""

import json
import sys
from pathlib import Path

import anyio
import pytest

from vetromar.errors import ConfigError
from vetromar.sources import client as sources_client
from vetromar.sources.catalog import CATALOG, catalog_entry
from vetromar.sources.registry import (
    SourceConfig,
    get_source,
    load_sources,
    remove_source,
    save_sources,
    upsert_source,
)

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _fake_source(**overrides) -> SourceConfig:
    kwargs = dict(
        name="fakechat",
        source_kind="chat",
        command=sys.executable,
        args=[str(FAKE_SERVER)],
    )
    kwargs.update(overrides)
    return SourceConfig(**kwargs)


# -- registry ----------------------------------------------------------------


def test_registry_roundtrip(tmp_path):
    path = tmp_path / "sources.toml"
    remote = SourceConfig(name="notion", url="https://mcp.notion.com/mcp", source_kind="document")
    local = _fake_source(env={"FAKE_CHAT_EXTRA_JSON": "[]"}, tools=["list_messages"])
    save_sources([remote, local], path)

    loaded = load_sources(path)
    assert [s.name for s in loaded] == ["notion", "fakechat"]
    assert loaded[0].transport == "http"
    assert loaded[1].transport == "stdio"
    assert loaded[1].env == {"FAKE_CHAT_EXTRA_JSON": "[]"}
    assert loaded[1].tools == ["list_messages"]

    upsert_source(SourceConfig(name="notion", url="https://elsewhere/mcp"), path)
    assert get_source("notion", path).url == "https://elsewhere/mcp"
    assert len(load_sources(path)) == 2

    remove_source("fakechat", path)
    assert [s.name for s in load_sources(path)] == ["notion"]
    with pytest.raises(ConfigError):
        remove_source("fakechat", path)


def test_source_needs_exactly_one_transport():
    with pytest.raises(ValueError):
        SourceConfig(name="bad")
    with pytest.raises(ValueError):
        SourceConfig(name="bad", url="https://x", command="y")


def test_missing_source_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        get_source("nope", tmp_path / "none.toml")


# -- catalog -----------------------------------------------------------------


KNOWN_KINDS = {"document", "ticket", "chat", "email", "metric_pull"}


def test_catalog_entries_are_remote_and_named():
    assert CATALOG, "catalog must not be empty"
    for entry in CATALOG:
        assert entry.url.startswith("https://")
        assert entry.name.islower()
        assert entry.source_kind in KNOWN_KINDS
    names = [e.name for e in CATALOG]
    assert len(names) == len(set(names)), "catalog names must be unique"
    assert catalog_entry("notion") is not None
    assert catalog_entry("definitely-not-real") is None


def test_catalog_credential_entries_carry_setup_url():
    # Providers without DCR (Slack-class) must point the customer at their
    # create-an-app console; one-click entries must not.
    for entry in CATALOG:
        if entry.needs_client_registration:
            assert entry.setup_url and entry.setup_url.startswith("https://")
    for expected in (
        "slack",
        "github",
        "hubspot",
        "box",
        "gmail",
        "google-drive",
        "google-calendar",
        "google-docs",
        "google-sheets",
        "google-chat",
    ):
        assert catalog_entry(expected).needs_client_registration
    assert catalog_entry("posthog") is not None
    assert not catalog_entry("posthog").needs_client_registration


# -- token storage -----------------------------------------------------------


def test_file_token_storage_roundtrip(tmp_path, monkeypatch):
    from vetromar.sources import auth

    monkeypatch.setattr(auth, "VETROMAR_HOME", tmp_path)
    monkeypatch.setattr(auth, "tokens_dir", lambda: tmp_path / "tokens")
    from mcp.shared.auth import OAuthToken

    storage = auth.FileTokenStorage("notion")
    assert anyio.run(storage.get_tokens) is None

    async def put():
        await storage.set_tokens(OAuthToken(access_token="secret-abc", token_type="Bearer"))

    anyio.run(put)
    token_file = tmp_path / "tokens" / "notion.json"
    assert token_file.exists()
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert anyio.run(storage.get_tokens).access_token == "secret-abc"
    storage.clear()
    assert anyio.run(storage.get_tokens) is None


# -- pre-registered clients (no-DCR providers) --------------------------------


def test_seed_client_info_roundtrip(tmp_path, monkeypatch):
    from vetromar.sources import auth

    monkeypatch.setattr(auth, "VETROMAR_HOME", tmp_path)
    assert not auth.has_client_info("slack")
    auth.seed_client_info("slack", " my-id ", " my-secret ")
    assert auth.has_client_info("slack")

    token_file = tmp_path / "tokens" / "slack.json"
    assert token_file.stat().st_mode & 0o777 == 0o600

    storage = auth.FileTokenStorage("slack")
    # get_client_info returning info is what makes OAuthClientProvider skip
    # DCR — the whole mechanism.
    info = anyio.run(storage.get_client_info)
    assert info.client_id == "my-id"
    assert info.client_secret == "my-secret"
    assert info.token_endpoint_auth_method == "client_secret_post"
    # Seeded clients register the HTTPS relay (Slack refuses localhost).
    assert list(map(str, info.redirect_uris)) == [auth.relay_redirect_uri()]
    assert auth.relay_redirect_uri().startswith("https://")


def test_oauth_provider_uses_stored_redirect_when_seeded(tmp_path, monkeypatch):
    from vetromar.sources import auth

    monkeypatch.setattr(auth, "VETROMAR_HOME", tmp_path)
    # No client info on disk -> DCR flow on the localhost loopback.
    provider = auth.build_oauth_provider("notion", "https://mcp.notion.com/mcp")
    assert [str(u) for u in provider.context.client_metadata.redirect_uris] == [
        auth.callback_redirect_uri()
    ]
    # Seeded credentials -> the provider must send the relay the app
    # registered, in both the authorize request and the token exchange.
    auth.seed_client_info("slack", "app-id", "app-secret")
    provider = auth.build_oauth_provider("slack", "https://mcp.slack.com/mcp")
    assert [str(u) for u in provider.context.client_metadata.redirect_uris] == [
        auth.relay_redirect_uri()
    ]


def test_seed_client_info_without_secret_is_public_client(tmp_path, monkeypatch):
    from vetromar.sources import auth

    monkeypatch.setattr(auth, "VETROMAR_HOME", tmp_path)
    auth.seed_client_info("slack", "id-only", None)
    info = anyio.run(auth.FileTokenStorage("slack").get_client_info)
    assert info.client_secret is None
    assert info.token_endpoint_auth_method == "none"


def test_seed_client_info_drops_stale_tokens(tmp_path, monkeypatch):
    from vetromar.sources import auth

    monkeypatch.setattr(auth, "VETROMAR_HOME", tmp_path)
    from mcp.shared.auth import OAuthToken

    storage = auth.FileTokenStorage("slack")

    async def put():
        await storage.set_tokens(OAuthToken(access_token="old", token_type="Bearer"))

    anyio.run(put)
    auth.seed_client_info("slack", "new-id", "new-secret")
    # Tokens minted under the previous client are invalid with new credentials.
    assert anyio.run(storage.get_tokens) is None
    assert anyio.run(storage.get_client_info).client_id == "new-id"


def test_connect_refuses_no_dcr_source_without_credentials(tmp_path, monkeypatch):
    from vetromar import cli
    from vetromar.sources import auth

    monkeypatch.setattr(auth, "VETROMAR_HOME", tmp_path)
    with pytest.raises(ConfigError) as exc:
        cli._connect_one("slack", "https://mcp.slack.com/mcp", None, "chat")
    assert "pre-registered" in str(exc.value)
    assert "api.slack.com" in exc.value.hint


def test_connect_no_dcr_source_with_credentials_seeds_then_connects(tmp_path, monkeypatch):
    from vetromar import cli
    from vetromar.sources import auth
    import vetromar.sources.registry as registry_mod

    monkeypatch.setattr(auth, "VETROMAR_HOME", tmp_path)
    monkeypatch.setattr(registry_mod, "VETROMAR_HOME", tmp_path)
    monkeypatch.setattr(
        sources_client, "test_source", lambda source, cancel_event=None: ["list_messages"]
    )
    cli._connect_one("slack", "https://mcp.slack.com/mcp", None, "chat", "app-id", "app-secret")
    assert auth.has_client_info("slack")
    assert get_source("slack", tmp_path / "sources.toml").url == "https://mcp.slack.com/mcp"


# -- OAuth consent cancellation ----------------------------------------------


def test_callback_server_cancel_aborts_wait_and_frees_port():
    import threading

    from vetromar.sources.auth import _CallbackServer

    server = _CallbackServer(port=14747)  # oddball test port, not the real 4747
    cancel = threading.Event()
    outcome: list[BaseException] = []

    def waiter():
        try:
            server.wait(timeout=10.0, cancel_event=cancel)
        except ConfigError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=waiter)
    thread.start()
    cancel.set()
    thread.join(timeout=3.0)
    assert not thread.is_alive(), "wait() did not return after cancel"
    assert outcome and "cancelled" in outcome[0].message.lower()

    # the callback port is released, so an immediate retry can bind it
    retry = _CallbackServer(port=14747)
    retry._httpd.shutdown()
    retry._httpd.server_close()


# -- live stdio session against the fake server ------------------------------


def test_open_session_stdio_discovers_tools():
    names = sources_client.test_source(_fake_source())
    assert {"list_channels", "list_messages", "post_message"} <= set(names)


def test_stdio_tool_call_roundtrip():
    async def run():
        async with sources_client.open_session(_fake_source()) as session:
            result = await session.call_tool("list_messages", {"channel": "eng", "after_ts": "100.1"})
            return json.loads(result.content[0].text)

    first = anyio.run(run)
    assert first["ts"] == "100.2"


def test_dead_command_is_config_error():
    bad = _fake_source(command="/nonexistent/binary", args=[])
    with pytest.raises(ConfigError):
        sources_client.test_source(bad)
