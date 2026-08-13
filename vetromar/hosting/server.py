"""The embedded graph host server: `cloud`'s FastAPI app run inside the
sidecar on a fixed, externally reachable port.

Deliberately a SECOND uvicorn, not a mount on the ui_server app: the UI API
binds 127.0.0.1 on an ephemeral port for the desktop shell, while members
need a stable port bound on all interfaces (port forwarding, tailnet,
LAN). Toggling hosting never touches the UI server.

SQLite-backed at ~/.vetromar/host/cloud.db — a mac mini needs no Postgres.
All `cloud` imports are lazy (the CONTRIBUTING.md carve-out): hosting is
optional and the import cost lands only on hosts.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from vetromar.config import VETROMAR_HOME

logger = logging.getLogger("vetromar.hosting")

HOST_DB_PATH = VETROMAR_HOME / "host" / "cloud.db"


def host_database_url() -> str:
    return "sqlite:///" + str(HOST_DB_PATH)


def _make_engine():
    from cloud.db import make_engine

    return make_engine(host_database_url())


def ensure_owner() -> str:
    """Enroll this machine's identity as the embedded server's owner —
    the boot handshake that lets the app create graphs on itself.
    Idempotent; returns the owner principal id."""
    from cloud.__main__ import set_owner

    from vetromar.identity import ensure_identity

    return set_owner(_make_engine(), ensure_identity().public_key)


class HostServer:
    """Lifecycle wrapper: one uvicorn server on a daemon thread."""

    def __init__(self) -> None:
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, port: int, bind: str = "0.0.0.0") -> None:
        if self.running:
            return
        import uvicorn

        from cloud.app import create_app

        ensure_owner()
        app = create_app(engine=_make_engine())
        config = uvicorn.Config(app, host=bind, port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self.port = port

        def _serve() -> None:
            try:
                self._server.run()
            except Exception:  # noqa: BLE001 — hosting must never kill the app
                logger.exception("embedded host server crashed")

        self._thread = threading.Thread(target=_serve, name="graph-host", daemon=True)
        self._thread.start()
        logger.info("embedded graph host serving on %s:%s", bind, port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self.port = None


# One host per process — the ui_server routes and run_server share it.
HOST = HostServer()
