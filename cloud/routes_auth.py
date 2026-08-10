"""Signup, login, me, invites. The unauthenticated credential routes are

rate-limited per client IP (see security.RateLimiter — deliberately minimal).
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import AuthContext, current_auth, get_session, me_payload, require_admin
from .models import (
    INVITE_DAYS,
    TOKEN_DAYS,
    Invite,
    Membership,
    ResetToken,
    Token,
    User,
    Workspace,
    new_id,
    utcnow,
)
from .security import (
    MIN_PASSWORD_LENGTH,
    RateLimiter,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)

router = APIRouter()


def _rate_limit(request: Request) -> None:
    # Per-app limiter (created lazily) so each service instance — and each
    # test app — has its own window.
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = request.app.state.rate_limiter = RateLimiter()
    key = request.client.host if request.client else "unknown"
    if not limiter.allow(key):
        raise HTTPException(429, "too many attempts, slow down")


def _normalize_email(email: str) -> str:
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        raise HTTPException(400, "invalid email address")
    return email


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            400, f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )


def _issue_token(session: Session, user: User) -> str:
    raw = generate_token()
    session.add(
        Token(
            id=new_id("tok"),
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=TOKEN_DAYS),
        )
    )
    return raw


class CreateWorkspace(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    email: str
    password: str


class Login(BaseModel):
    email: str
    password: str


class CreateInvite(BaseModel):
    role: str = "member"


class AcceptInvite(BaseModel):
    token: str
    name: str = Field(min_length=1, max_length=200)
    email: str
    password: str


@router.post("/v1/workspaces", status_code=201)
def create_workspace(
    body: CreateWorkspace, request: Request, session: Session = Depends(get_session)
):
    _rate_limit(request)
    _check_password(body.password)
    email = _normalize_email(body.email)
    if session.scalar(select(User).where(User.email == email)):
        raise HTTPException(400, "an account with this email already exists")
    user = User(
        id=new_id("usr"),
        email=email,
        password_hash=hash_password(body.password),
        name=body.name.strip(),
    )
    workspace = Workspace(id=new_id("ws"), name=body.workspace_name.strip())
    membership = Membership(
        id=new_id("mem"), user_id=user.id, workspace_id=workspace.id, role="admin"
    )
    # Flush parents before FK'd rows: without ORM relationships SQLAlchemy
    # gives no cross-table insert ordering, and Postgres enforces the FKs
    # at flush (SQLite's default-off enforcement masked this).
    session.add_all([user, workspace])
    session.flush()
    session.add(membership)
    raw = _issue_token(session, user)
    session.flush()
    auth = AuthContext(user=user, workspace=workspace, membership=membership)
    return {"token": raw, **me_payload(auth)}


@router.post("/v1/auth/login")
def login(body: Login, request: Request, session: Session = Depends(get_session)):
    _rate_limit(request)
    email = _normalize_email(body.email)
    user = session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(
        user.password_hash, body.password
    ):
        raise HTTPException(401, "invalid email or password")
    membership = session.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.is_active.is_(True)
        )
    )
    if membership is None:
        raise HTTPException(403, "no active workspace membership")
    workspace = session.get(Workspace, membership.workspace_id)
    raw = _issue_token(session, user)
    auth = AuthContext(user=user, workspace=workspace, membership=membership)
    return {"token": raw, **me_payload(auth)}


@router.get("/v1/me")
def me(auth: AuthContext = Depends(current_auth)):
    return me_payload(auth)


@router.post("/v1/invites", status_code=201)
def create_invite(
    body: CreateInvite,
    auth: AuthContext = Depends(require_admin),
    session: Session = Depends(get_session),
):
    if body.role not in ("member", "admin"):
        raise HTTPException(400, "role must be 'member' or 'admin'")
    raw = generate_token()
    expires = utcnow() + timedelta(days=INVITE_DAYS)
    session.add(
        Invite(
            id=new_id("inv"),
            workspace_id=auth.workspace.id,
            token_hash=hash_token(raw),
            role=body.role,
            created_by=auth.user.id,
            expires_at=expires,
        )
    )
    # Raw token is returned exactly once; only its hash is stored. The link
    # is copyable from the app — the server never emails it.
    return {
        "token": raw,
        "url_path": f"/invite-accept?token={raw}",
        "expires_at": expires.isoformat() + "Z",
        "workspace": auth.workspace.name,
    }


@router.post("/v1/invites/accept", status_code=201)
def accept_invite(
    body: AcceptInvite, request: Request, session: Session = Depends(get_session)
):
    _rate_limit(request)
    _check_password(body.password)
    invite = session.scalar(
        select(Invite).where(Invite.token_hash == hash_token(body.token))
    )
    now = utcnow()
    if invite is None or invite.expires_at <= now:
        raise HTTPException(400, "invite link is invalid or has expired")
    if invite.accepted_at is not None:
        raise HTTPException(400, "invite link has already been used")
    email = _normalize_email(body.email)
    if session.scalar(select(User).where(User.email == email)):
        raise HTTPException(400, "an account with this email already exists")
    workspace = session.get(Workspace, invite.workspace_id)
    user = User(
        id=new_id("usr"),
        email=email,
        password_hash=hash_password(body.password),
        name=body.name.strip(),
    )
    membership = Membership(
        id=new_id("mem"), user_id=user.id, workspace_id=workspace.id, role=invite.role
    )
    invite.accepted_at = now
    invite.accepted_by = user.id
    # Same parent-first flush as create_workspace (Postgres FK ordering).
    session.add(user)
    session.flush()
    session.add(membership)
    session.flush()
    return {"workspace": workspace.name, "email": email}


class ResetConfirm(BaseModel):
    token: str
    password: str


@router.post("/v1/auth/reset-confirm")
def reset_confirm(
    body: ResetConfirm, request: Request, session: Session = Depends(get_session)
):
    _rate_limit(request)
    _check_password(body.password)
    reset = session.scalar(
        select(ResetToken).where(ResetToken.token_hash == hash_token(body.token))
    )
    now = utcnow()
    if reset is None or reset.expires_at <= now or reset.used_at is not None:
        raise HTTPException(400, "reset link is invalid or has expired")
    user = session.get(User, reset.user_id)
    if user is None or not user.is_active:
        raise HTTPException(400, "reset link is invalid or has expired")
    user.password_hash = hash_password(body.password)
    reset.used_at = now
    # The point of a reset: every existing session dies with the old password.
    from sqlalchemy import delete

    session.execute(delete(Token).where(Token.user_id == user.id))
    return {"ok": True}
