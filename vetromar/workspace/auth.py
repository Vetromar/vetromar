"""Workspace session: sign-in/out and the local session cache.

Mirrors the operations.validate_and_save_api_key idiom: live-validate the
credential against the service, persist the secret to a 0600 file, cache the
non-secret session facts as JSON. `status()` never touches the network —
required sign-in must not become a hard network dependency (cached session
works offline).
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Optional

from vetromar.config import (
    Config,
    clear_cloud_token,
    load_config,
    save_cloud_token,
    workspace_cache_path,
)

from .client import CloudClient, WorkspaceError


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


def _cache_session(body: dict) -> dict:
    cache = _load_cache()
    cache.update(
        {
            "email": body["user"]["email"],
            "user_id": body["user"]["id"],
            "user_name": body["user"]["name"],
            "workspace_id": body["workspace"]["id"],
            "workspace_name": body["workspace"]["name"],
            "role": body["role"],
        }
    )
    # Pre-pivot cache fields from older builds — drop on the next write.
    for stale in ("trial", "billing", "locked"):
        cache.pop(stale, None)
    _save_cache(cache)
    return cache


def sign_in(email: str, password: str, config: Optional[Config] = None) -> dict:
    """Live login against the cloud service; persists token + session cache.
    Raises WorkspaceError (invalid credentials, unreachable service)."""
    config = config or load_config()
    client = CloudClient(config.cloud_api_url)
    try:
        body = client.login(email, password)
        save_cloud_token(body["token"])
        cache = _cache_session(body)
        client.token = body["token"]
        try:
            client.register_device(device_id(), name=socket.gethostname())
        except WorkspaceError:
            pass  # device registration is best-effort; sync re-registers
    finally:
        client.close()
    return status_from_cache(cache)


def sign_out() -> None:
    clear_cloud_token()
    path = workspace_cache_path()
    if path.exists():
        path.unlink()


def status_from_cache(cache: Optional[dict] = None) -> dict:
    cache = cache if cache is not None else _load_cache()
    from vetromar.config import load_config as _lc

    signed_in = bool(_lc().cloud_token and cache.get("workspace_id"))
    return {
        "signed_in": signed_in,
        "email": cache.get("email"),
        "user_id": cache.get("user_id"),
        "workspace": cache.get("workspace_name"),
        "workspace_id": cache.get("workspace_id"),
        "role": cache.get("role"),
        "device_id": cache.get("device_id"),
        "last_synced_at": cache.get("last_synced_at"),
    }


def status() -> dict:
    """Local-only session status — never blocks on the network."""
    return status_from_cache()


def refresh_status(config: Optional[Config] = None) -> dict:
    """Re-fetch /me (role/name changes). Falls back to the cache when
    offline; a 401 clears the session (revoked/expired)."""
    config = config or load_config()
    if not config.cloud_token:
        return status()
    client = CloudClient(config.cloud_api_url, token=config.cloud_token)
    try:
        body = client.me()
    except WorkspaceError as exc:
        from .client import NotSignedIn

        if isinstance(exc, NotSignedIn):
            sign_out()
        return status()
    finally:
        client.close()
    cache = _cache_session(body)
    return status_from_cache(cache)


def note_synced(now_iso: str) -> None:
    cache = _load_cache()
    cache["last_synced_at"] = now_iso
    _save_cache(cache)
