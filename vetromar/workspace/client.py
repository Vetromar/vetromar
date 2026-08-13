"""HTTP client for a graph host server. Transport is injectable so tests run

against the in-process FastAPI app (httpx.ASGITransport) — the same code path
a real deployment uses over the network.

One client instance addresses one (server, workspace) pair: the workspace id
rides on every request as `X-Workspace-Id`. Auth is the keypair challenge
flow — `login_with_key` trades a signed nonce for a short-lived bearer token
held in memory only (nothing token-shaped ever touches disk)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

import httpx

if TYPE_CHECKING:
    from vetromar.identity import Identity

logger = logging.getLogger("vetromar.workspace.client")


class WorkspaceError(Exception):
    """Host server unreachable or request rejected — user-renderable."""


class NotSignedIn(WorkspaceError):
    """401 — token missing/expired/revoked. Callers re-run the challenge flow."""


class WorkspaceBindingError(WorkspaceError):
    """This store last synced with a DIFFERENT workspace — a human must
    decide (upload the local graph, or hold off) before sync runs."""


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:  # noqa: BLE001
        detail = None
    return detail or f"HTTP {resp.status_code}"


class CloudClient:
    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        workspace_id: Optional[str] = None,
        http: Optional[httpx.Client] = None,
    ):
        self.token = token
        self.workspace_id = workspace_id
        self._http = http or httpx.Client(base_url=base_url, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.workspace_id:
            headers["X-Workspace-Id"] = self.workspace_id
        try:
            resp = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # The raw httpx text (URLs, socket errors) stays in the log; the
            # user just needs to know the host is unreachable.
            logger.warning("host request %s %s failed: %s", method, path, exc)
            raise WorkspaceError(
                "could not reach the graph's host — check your connection "
                "(and that the host is online) and try again (error VM-300)"
            ) from exc
        if resp.status_code == 401:
            raise NotSignedIn(_detail(resp))
        if resp.status_code >= 400:
            raise WorkspaceError(_detail(resp))
        return resp.json() if resp.content else {}

    # -- keypair auth ----------------------------------------------------------

    def challenge(self, public_key: str) -> str:
        return self._request(
            "POST", "/v1/auth/challenge", json={"public_key": public_key}
        )["nonce"]

    def key_proof(self, identity: "Identity") -> dict:
        """A freshly signed challenge — enrollment, sign-in, and destructive
        confirmations all consume one of these."""
        nonce = self.challenge(identity.public_key)
        return {"nonce": nonce, "signature": identity.sign(nonce)}

    def login_with_key(self, identity: "Identity") -> dict:
        """Challenge → sign → token. The token lives on this instance only."""
        proof = self.key_proof(identity)
        body = self._request(
            "POST",
            "/v1/auth/verify",
            json={"public_key": identity.public_key, **proof},
        )
        self.token = body["token"]
        return body

    def accept_invite(
        self, invite_token: str, identity: "Identity", handle: str, display_name: str
    ) -> dict:
        """Enroll this identity via an invite. Proves key possession with a
        challenge; the response carries a session token (joining IS signing
        in) plus the membership."""
        proof = self.key_proof(identity)
        body = self._request(
            "POST",
            "/v1/invites/accept",
            json={
                "token": invite_token,
                "public_key": identity.public_key,
                "handle": handle,
                "display_name": display_name,
                **proof,
            },
        )
        self.token = body["token"]
        return body

    # -- identity + workspaces -------------------------------------------------

    def me(self) -> dict:
        return self._request("GET", "/v1/me")

    def list_workspaces(self) -> dict:
        return self._request("GET", "/v1/workspaces")

    def create_workspace(self, name: str, handle: str, display_name: str) -> dict:
        """Server-owner only: create a graph on this host."""
        return self._request(
            "POST",
            "/v1/workspaces",
            json={"name": name, "handle": handle, "display_name": display_name},
        )

    # -- members ----------------------------------------------------------------

    def members(self) -> dict:
        return self._request("GET", "/v1/members")

    def create_invite(self, role: str = "member") -> dict:
        return self._request("POST", "/v1/invites", json={"role": role})

    def remove_member(self, principal_id: str) -> None:
        self._request("DELETE", f"/v1/members/{principal_id}")

    def set_role(self, principal_id: str, role: str) -> dict:
        return self._request(
            "POST", f"/v1/members/{principal_id}/role", json={"role": role}
        )

    def register_device(self, device_id: str, name: str = "") -> dict:
        return self._request("PUT", f"/v1/devices/{device_id}", json={"name": name})

    # -- deletion ----------------------------------------------------------------

    def delete_workspace(self, identity: "Identity") -> dict:
        return self._request("DELETE", "/v1/workspaces", json=self.key_proof(identity))

    def delete_identity(self, identity: "Identity") -> dict:
        return self._request("DELETE", "/v1/me", json=self.key_proof(identity))

    # -- sync --------------------------------------------------------------------

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
