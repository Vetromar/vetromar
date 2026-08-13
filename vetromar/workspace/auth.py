"""The per-installation device id.

All that survives of the account-era session module: sync attribution needs
a stable device id per installation (the replication protocol skips your own
echoes by it). Identity itself lives in vetromar/identity.py — a keypair,
not a session — and per-graph connection state lives in the graph registry.
"""

from __future__ import annotations

import json
import uuid

from vetromar.config import workspace_cache_path


def _load_cache() -> dict:
    path = workspace_cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    path = workspace_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2) + "\n")


def device_id() -> str:
    """Stable per-installation device id, minted on first use."""
    cache = _load_cache()
    if not cache.get("device_id"):
        cache["device_id"] = f"dev_{uuid.uuid4().hex[:12]}"
        _save_cache(cache)
    return cache["device_id"]
