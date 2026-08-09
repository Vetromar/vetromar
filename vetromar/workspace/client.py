"""HTTP client for the cloud service. Transport is injectable so tests run

against the in-process FastAPI app (httpx.ASGITransport) — the same code path
a real deployment uses over the network.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("vetromar.workspace.client")


class WorkspaceError(Exception):
    """Cloud service unreachable or request rejected — user-renderable."""


class NotSignedIn(WorkspaceError):
    """401 — token missing/expired/revoked. The app returns to sign-in."""


class WorkspaceBindingError(WorkspaceError):
    """This machine's store last synced with a DIFFERENT workspace — a human
    must decide (upload the local graph, or hold off) before sync runs."""


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:  # noqa: BLE001
        detail = None
    if isinstance(detail, dict):
        return detail.get("reason", str(detail))
    return detail or f"HTTP {resp.status_code}"


class CloudClient:
    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        http: Optional[httpx.Client] = None,
    ):
        self.token = token
        self._http = http or httpx.Client(base_url=base_url, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # The raw httpx text (URLs, socket errors) stays in the log; the
            # user just needs to know the service is unreachable.
            logger.warning("cloud request %s %s failed: %s", method, path, exc)
            raise WorkspaceError(
                "could not reach the workspace service — check your "
                "connection and try again (error VM-300)"
            ) from exc
        if resp.status_code == 401:
            raise NotSignedIn(_detail(resp))
        if resp.status_code >= 400:
            raise WorkspaceError(_detail(resp))
        return resp.json() if resp.content else {}

    # -- accounts ------------------------------------------------------------

    def signup(self, workspace_name: str, name: str, email: str, password: str) -> dict:
        return self._request(
            "POST",
            "/v1/workspaces",
            json={
                "workspace_name": workspace_name,
                "name": name,
                "email": email,
                "password": password,
            },
        )

    def login(self, email: str, password: str) -> dict:
        return self._request(
            "POST", "/v1/auth/login", json={"email": email, "password": password}
        )

    def me(self) -> dict:
        return self._request("GET", "/v1/me")

    def members(self) -> dict:
        return self._request("GET", "/v1/members")

    def create_invite(self, role: str = "member", email: Optional[str] = None) -> dict:
        body: dict = {"role": role}
        if email:
            body["email"] = email
        return self._request("POST", "/v1/invites", json=body)

    def reset_request(self, email: str) -> dict:
        return self._request("POST", "/v1/auth/reset-request", json={"email": email})

    def accept_invite(self, token: str, name: str, email: str, password: str) -> dict:
        return self._request(
            "POST",
            "/v1/invites/accept",
            json={
                "token": token,
                "name": name,
                "email": email,
                "password": password,
            },
        )

    def remove_member(self, user_id: str) -> None:
        self._request("DELETE", f"/v1/members/{user_id}")

    def register_device(self, device_id: str, name: str = "") -> dict:
        return self._request("PUT", f"/v1/devices/{device_id}", json={"name": name})

    # -- deletion ------------------------------------------------------------

    def delete_workspace(self, password: str) -> dict:
        return self._request("DELETE", "/v1/workspaces", json={"password": password})

    def delete_account(self, password: str) -> dict:
        return self._request("DELETE", "/v1/me", json={"password": password})

    # -- sync ----------------------------------------------------------------

    def push(self, device_id: str, changes: list[dict]) -> dict:
        return self._request(
            "POST",
            "/v1/sync/push",
            json={"device_id": device_id, "changes": changes},
        )

    def pull(self, since: int, limit: int = 500) -> dict:
        return self._request(
            "GET", "/v1/sync/pull", params={"since": since, "limit": limit}
        )
