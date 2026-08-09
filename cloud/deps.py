"""Request dependencies: DB session, bearer-token auth, role guards."""

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
    Token,
    User,
    Workspace,
    utcnow,
)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session
        session.commit()


@dataclass
class AuthContext:
    user: User
    workspace: Workspace
    membership: Membership

    @property
    def role(self) -> str:
        return self.membership.role


def auth_from_raw_token(session: Session, raw: str) -> AuthContext:
    """Token → AuthContext, with the sliding-expiry bump in one place."""
    from .security import hash_token

    token = session.scalar(select(Token).where(Token.token_hash == hash_token(raw)))
    now = utcnow()
    if token is None or token.expires_at <= now:
        raise HTTPException(401, "invalid or expired token")
    user = session.get(User, token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "account disabled")
    membership = session.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.is_active.is_(True)
        )
    )
    if membership is None:
        raise HTTPException(403, "no active workspace membership")
    workspace = session.get(Workspace, membership.workspace_id)
    if workspace is None:
        raise HTTPException(403, "workspace not found")
    # Sliding expiry: any authenticated request keeps the session alive.
    token.expires_at = now + timedelta(days=TOKEN_DAYS)
    token.last_used_at = now
    return AuthContext(user=user, workspace=workspace, membership=membership)


def current_auth(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
) -> AuthContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return auth_from_raw_token(session, authorization.removeprefix("Bearer ").strip())


def require_admin(auth: AuthContext = Depends(current_auth)) -> AuthContext:
    if auth.role != "admin":
        raise HTTPException(403, "admin role required")
    return auth


def me_payload(auth: AuthContext) -> dict:
    return {
        "user": {"id": auth.user.id, "email": auth.user.email, "name": auth.user.name},
        "workspace": {
            "id": auth.workspace.id,
            "name": auth.workspace.name,
            "created_at": auth.workspace.created_at.isoformat() + "Z",
        },
        "role": auth.role,
    }
