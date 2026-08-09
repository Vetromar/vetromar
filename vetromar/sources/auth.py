"""Generic MCP OAuth — one implementation for every compliant remote server.

The whole point: ZERO per-source auth code. The MCP authorization spec
(OAuth 2.1 + PKCE + dynamic client registration + protected-resource
discovery) lets one client implementation authorize against any provider.
The UX is a single browser consent click: we open the provider's consent
page and catch the redirect on a localhost loopback server. Tokens land in
`~/.vetromar/tokens/<source>.json` (0600 — precedent: the credentials file);
the customer never sees or pastes one.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from vetromar.config import VETROMAR_HOME
from vetromar.errors import ConfigError

DEFAULT_CALLBACK_PORT = 4747


def _callback_port() -> int:
    return int(os.environ.get("VETROMAR_OAUTH_PORT", DEFAULT_CALLBACK_PORT))


def callback_redirect_uri() -> str:
    """The loopback redirect URI used for dynamic client registration."""
    return f"http://localhost:{_callback_port()}/callback"


DEFAULT_OAUTH_RELAY = "https://vetromar.com/oauth-callback.html"


def relay_redirect_uri() -> str:
    """The HTTPS redirect URI for customer-registered apps (no-DCR
    providers): some of them — Slack notably — refuse http://localhost
    redirect URLs, so consent redirects land on this static vetromar.com
    page, which immediately bounces the authorization response to the local
    callback server. This is the URL customers must configure on their app."""
    return os.environ.get("VETROMAR_OAUTH_RELAY", DEFAULT_OAUTH_RELAY)


def tokens_dir() -> Path:
    return VETROMAR_HOME / "tokens"


class FileTokenStorage(TokenStorage):
    """Per-source token + registered-client persistence, 0600 on disk."""

    def __init__(self, source_name: str):
        self._path = tokens_dir() / f"{source_name}.json"

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(mode=0o600, exist_ok=True)
        self._path.chmod(0o600)
        self._path.write_text(json.dumps(data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = json.loads(tokens.model_dump_json())
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = json.loads(client_info.model_dump_json())
        self._write(data)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


def seed_client_info(source_name: str, client_id: str, client_secret: str | None) -> None:
    """Pre-register an OAuth client for a source whose provider has no
    dynamic client registration (Slack-class servers). The customer creates
    their own app with the provider once and pastes its credentials; we
    store them as the client info the SDK would otherwise obtain via DCR —
    `OAuthClientProvider` skips registration whenever storage already holds
    client info, and the rest of the flow (discovery, consent, tokens) stays
    the one generic implementation."""
    info = OAuthClientInformationFull(
        client_id=client_id.strip(),
        client_secret=client_secret.strip() if client_secret else None,
        # Seeded clients always redirect via the HTTPS relay page — one
        # consistent URL to configure, and it works for providers that
        # refuse http://localhost (Slack). The relay bounces straight back
        # to the local callback server.
        redirect_uris=[relay_redirect_uri()],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post" if client_secret else "none",
        client_name="Vetromar",
    )
    storage = FileTokenStorage(source_name)
    data = storage._read()
    data["client_info"] = json.loads(info.model_dump_json())
    # Fresh credentials invalidate any tokens minted under the old client.
    data.pop("tokens", None)
    storage._write(data)


def has_client_info(source_name: str) -> bool:
    """True when a registered client (seeded or DCR-cached) exists on disk."""
    return bool(FileTokenStorage(source_name)._read().get("client_info"))


class _CallbackServer:
    """Loopback HTTP server that catches the provider's redirect with the
    authorization code. Fixed port so the registered redirect URI is stable
    across connects (dynamic registration is cached per source)."""

    def __init__(self, port: int):
        self.port = port
        self._result: tuple[str, str | None] | None = None
        self._done = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                params = parse_qs(urlparse(self.path).query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                if code:
                    server._result = (code, state)
                    body = b"<h2>Vetromar is connected.</h2>You can close this tab."
                else:
                    body = b"<h2>Authorization failed.</h2>" + self.path.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)
                if code:
                    server._done.set()

            def log_message(self, *args) -> None:
                pass

        try:
            self._httpd = HTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise ConfigError(
                f"Could not open the OAuth callback port {port}: {exc}",
                hint="Free the port or set VETROMAR_OAUTH_PORT to another one.",
            ) from exc
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def wait(
        self, timeout: float = 300.0, cancel_event: threading.Event | None = None
    ) -> tuple[str, str | None]:
        """Block until the consent redirect lands, the timeout passes, or
        `cancel_event` is set (the desktop UI's Cancel button). Always frees
        the callback port on the way out so a retry can bind it again."""
        try:
            deadline = time.monotonic() + timeout
            while not self._done.wait(0.2):
                if cancel_event is not None and cancel_event.is_set():
                    raise ConfigError(
                        "Authorization cancelled.",
                        hint="Run connect again to retry.",
                    )
                if time.monotonic() >= deadline:
                    raise ConfigError(
                        "Timed out waiting for the browser authorization.",
                        hint="Re-run the command and complete the consent page within 5 minutes.",
                    )
            assert self._result is not None
            return self._result
        finally:
            self._httpd.shutdown()
            self._httpd.server_close()  # shutdown() alone leaves the port bound

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}/callback"


def build_oauth_provider(
    source_name: str,
    server_url: str,
    cancel_event: threading.Event | None = None,
) -> OAuthClientProvider:
    """The one generic auth object: DCR + PKCE + browser consent + loopback
    callback. Reused tokens make later connects/syncs silent — the callback
    server only binds its port when a real browser consent is needed.
    `cancel_event` (optional) aborts the consent wait early."""
    port = _callback_port()
    pending: dict[str, _CallbackServer] = {}

    # The SDK sends client_metadata.redirect_uris[0] in both the authorize
    # request and the token exchange, so it must match what the client
    # registered: seeded clients (no-DCR providers) registered the HTTPS
    # relay; DCR clients registered the localhost loopback. Either way the
    # response ends up at the local callback server — the relay page just
    # adds one HTTPS hop for providers that require it.
    stored = FileTokenStorage(source_name)._read().get("client_info") or {}
    redirect_uri = (stored.get("redirect_uris") or [f"http://localhost:{port}/callback"])[0]

    async def redirect_handler(authorization_url: str) -> None:
        pending["server"] = _CallbackServer(port)
        print(f"Opening browser for authorization: {authorization_url}")
        webbrowser.open(authorization_url)

    async def callback_handler() -> tuple[str, str | None]:
        server = pending.pop("server", None)
        if server is None:
            raise ConfigError("OAuth callback awaited before any redirect happened.")
        return server.wait(cancel_event=cancel_event)

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="Vetromar",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=FileTokenStorage(source_name),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
