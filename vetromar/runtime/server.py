"""Ollama server lifecycle — reuse a running one, else start the managed binary.

The extraction client (`ollama.chat` in local_backend.py) talks HTTP to
`ollama_host`; all this module guarantees is that *a* server is answering there.
If one already is (a system Ollama, this dev machine), we reuse it untouched. If
not, we spawn the resolved binary as `ollama serve` with `OLLAMA_MODELS` pointed
at Vetromar's managed models dir, and wait for it to come up.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request

from vetromar.errors import ConfigError
from vetromar.runtime.binary import resolve_binary

_STARTUP_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.5


def _base_url(config) -> str:
    host = config.ollama_host
    if "://" not in host:
        host = f"http://{host}"
    return host.rstrip("/")


def server_up(config) -> bool:
    """True if an Ollama server answers /api/version at ollama_host."""
    try:
        with urllib.request.urlopen(f"{_base_url(config)}/api/version", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_server(config) -> None:
    """Guarantee a server on ollama_host, starting the managed binary if needed."""
    if server_up(config):
        return

    binary = resolve_binary(config)
    if binary is None:
        raise ConfigError(
            "The local AI runtime isn't installed yet.",
            hint="Open Settings → Download local models (or run `vetromar setup` "
            "from a terminal) to install it.",
        )

    env = {**os.environ, "OLLAMA_MODELS": str(config.models_dir)}
    if "://" in config.ollama_host or ":" in config.ollama_host:
        env["OLLAMA_HOST"] = config.ollama_host
    config.models_dir.mkdir(parents=True, exist_ok=True)

    try:
        # start_new_session detaches the server into its own process group so it
        # outlives the short-lived CLI process that spawned it — a later
        # `vetromar capture` reuses this same server instead of respawning.
        subprocess.Popen(
            [str(binary), "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise ConfigError(
            f"Could not start the Ollama server ({exc}).",
            hint="Run `vetromar setup` to reinstall the runtime.",
        ) from exc

    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if server_up(config):
            return
        time.sleep(_POLL_INTERVAL_S)

    raise ConfigError(
        f"The Ollama server did not become ready within {int(_STARTUP_TIMEOUT_S)}s.",
        hint="Try `vetromar doctor` to diagnose, or re-run `vetromar setup`.",
    )
