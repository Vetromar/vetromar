"""Request dependencies: DB session, bearer-token auth, role guards.

Auth has two layers. A bearer token identifies a PRINCIPAL (a public key
that proved possession via the challenge flow). Workspace-scoped routes
additionally require the `X-Workspace-Id` header and resolve the principal's
membership there — one principal belongs to any number of workspaces on a
server, so the workspace is always the caller's explicit choice, never
inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    TOKEN_DAYS,
    Membership,
    Principal,
    Token,
    Workspace,
    utcnow,
)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session
        session.commit()


@dataclass
class AuthContext:
    principal: Principal
    workspace: Workspace
    membership: Membership

    @property
    def role(self) -> str:
        return self.membership.role


def principal_from_raw_token(session: Session, raw: str) -> Principal:
    """Token → Principal, with the sliding-expiry bump in one place."""
    from .security import hash_token

    token = session.scalar(select(Token).where(Token.token_hash == hash_token(raw)))
    now = utcnow()
    if token is None or token.expires_at <= now:
        raise HTTPException(401, "invalid or expired token")
    principal = session.get(Principal, token.user_id)
    if principal is None or not principal.is_active:
        raise HTTPException(401, "identity disabled")
    # Sliding expiry: any authenticated request keeps the session alive.
    token.expires_at = now + timedelta(days=TOKEN_DAYS)
    token.last_used_at = now
    return principal


def _raw_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization.removeprefix("Bearer ").strip()


def current_principal(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
) -> Principal:
    return principal_from_raw_token(session, _raw_bearer(authorization))


def membership_for(
    session: Session, principal: Principal, workspace_id: str
) -> tuple[Workspace, Membership]:
    membership = session.scalar(
        select(Membership).where(
            Membership.user_id == principal.id,
            Membership.workspace_id == workspace_id,
            Membership.is_active.is_(True),
        )
    )
    if membership is None:
        raise HTTPException(403, "not a member of this workspace")
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(403, "workspace not found")
    return workspace, membership


def current_auth(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None),
) -> AuthContext:
    principal = principal_from_raw_token(session, _raw_bearer(authorization))
    if not x_workspace_id:
        raise HTTPException(400, "missing X-Workspace-Id header")
    workspace, membership = membership_for(session, principal, x_workspace_id)
    return AuthContext(principal=principal, workspace=workspace, membership=membership)


def require_admin(auth: AuthContext = Depends(current_auth)) -> AuthContext:
    if auth.role not in ("host", "admin"):
        raise HTTPException(403, "admin role required")
    return auth


def require_host(auth: AuthContext = Depends(current_auth)) -> AuthContext:
    if auth.role != "host":
        raise HTTPException(403, "host role required")
    return auth


def membership_payload(membership: Membership, workspace: Workspace) -> dict:
    return {
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "role": membership.role,
        "handle": membership.handle,
        "display_name": membership.display_name,
    }


def me_payload(session: Session, principal: Principal) -> dict:
    rows = session.execute(
        select(Membership, Workspace)
        .join(Workspace, Workspace.id == Membership.workspace_id)
        .where(Membership.user_id == principal.id, Membership.is_active.is_(True))
        .order_by(Membership.created_at)
    ).all()
    return {
        "principal": {
            "id": principal.id,
            "public_key": principal.public_key,
            "is_owner": principal.is_owner,
        },
        "workspaces": [membership_payload(m, w) for m, w in rows],
    }
