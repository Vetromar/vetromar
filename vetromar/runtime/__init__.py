"""Managed Ollama runtime — zero-install local mode.

Provisioning only: this package owns the Ollama *binary*, *server*, and *model*
so the customer never installs Ollama or runs `ollama pull`. It never imports
the extraction path — it just guarantees the runtime that path will call is
present and answering.

Two entry points:
- `ensure_local_ready(config, progress)` — mutating; used by `vetromar setup`.
- `local_status(config)` — non-mutating snapshot; used by `vetromar doctor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from vetromar.runtime.binary import download_binary, resolve_binary
from vetromar.runtime.model import (
    Progress,
    ensure_model,
    model_files_present,
    model_present,
)
from vetromar.runtime.server import ensure_server, server_up

__all__ = [
    "LocalStatus",
    "ensure_local_ready",
    "local_status",
    "resolve_binary",
    "server_up",
    "model_present",
    "model_files_present",
]


@dataclass
class LocalStatus:
    binary: bool  # an Ollama binary is resolvable (managed or system)
    server: bool  # a server answers on ollama_host
    model: bool  # the configured local_model is pulled

    @property
    def ready(self) -> bool:
        return self.binary and self.server and self.model


def ensure_local_ready(config, progress: Progress | None = None) -> None:
    """Install the runtime if missing, start the server, pull the model.

    Idempotent: on a machine that already has Ollama + the model this is a fast
    set of checks. Each step raises a friendly ConfigError on failure."""
    if resolve_binary(config) is None:
        download_binary(config, progress)
    ensure_server(config)
    ensure_model(config, progress)


def local_status(config) -> LocalStatus:
    """Read-only health snapshot. `model` is only checked when a server is up
    (the client cannot list models without one)."""
    binary = resolve_binary(config) is not None
    server = server_up(config)
    model = False
    if server:
        try:
            model = model_present(config)
        except Exception:  # noqa: BLE001 — doctor never crashes on a probe
            model = False
    return LocalStatus(binary=binary, server=server, model=model)
