"""Keypair auth, workspaces, invites, roles.

Sign-in is a signed challenge: POST /v1/auth/challenge hands out a
single-use nonce for a public key; POST /v1/auth/verify trades the signed
nonce for a bearer token (only for enrolled principals). Enrollment happens
exclusively through invites — accepting one also consumes a challenge, so
possession of the private key is proven at the door. The unauthenticated
routes are rate-limited per client IP (security.RateLimiter).
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import (
    AuthContext,
    current_principal,
    get_session,
    me_payload,
    membership_payload,
    require_admin,
    require_host,
)
from .models import (
    CHALLENGE_MINUTES,
    INVITE_DAYS,
    TOKEN_DAYS,
    AuthChallenge,
    Invite,
    Membership,
    Principal,
    Token,
    Workspace,
    new_id,
    utcnow,
)
from .security import (
    RateLimiter,
    generate_token,
    hash_token,
    verify_signature,
)

router = APIRouter()

MAX_PUBLIC_KEY_LEN = 64
HANDLE_MAX = 40


def _rate_limit(request: Request) -> None:
    # Per-app limiter (created lazily) so each service instance — and each
    # test app — has its own window.
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = request.app.state.rate_limiter = RateLimiter()
    key = request.client.host if request.client else "unknown"
    if not limiter.allow(key):
        raise HTTPException(429, "too many attempts, slow down")


def _normalize_handle(handle: str) -> str:
    handle = handle.strip().lstrip("@").lower()
    if not handle or len(handle) > HANDLE_MAX:
        raise HTTPException(400, "handle must be 1-40 characters")
    if not all(c.isalnum() or c in "-_." for c in handle):
        raise HTTPException(
            400, "handle may only contain letters, digits, dashes, dots, underscores"
        )
    return handle


def _issue_token(session: Session, principal: Principal) -> str:
    raw = generate_token()
    session.add(
        Token(
            id=new_id("tok"),
            user_id=principal.id,
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=TOKEN_DAYS),
        )
    )
    return raw


def consume_challenge(session: Session, public_key: str, nonce: str, signature: str) -> None:
    """Verify + burn a challenge: the caller proved possession of the key.
    Shared by sign-in, invite acceptance, and destructive-action proofs."""
    challenge = session.scalar(
        select(AuthChallenge).where(AuthChallenge.nonce_hash == hash_token(nonce))
    )
    now = utcnow()
    if (
        challenge is None
        or challenge.expires_at <= now
        or challenge.used_at is not None
        or challenge.public_key != public_key
    ):
        raise HTTPException(401, "challenge is invalid or has expired")
    if not verify_signature(public_key, nonce, signature):
        raise HTTPException(401, "signature does not verify")
    challenge.used_at = now


class ChallengeBody(BaseModel):
    public_key: str = Field(min_length=1, max_length=MAX_PUBLIC_KEY_LEN)


class VerifyBody(BaseModel):
    public_key: str = Field(min_length=1, max_length=MAX_PUBLIC_KEY_LEN)
    nonce: str
    signature: str


class CreateWorkspace(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    handle: str = Field(min_length=1, max_length=HANDLE_MAX)
    display_name: str = Field(min_length=1, max_length=200)


class CreateInvite(BaseModel):
    role: str = "member"


class AcceptInvite(BaseModel):
    token: str
    public_key: str = Field(min_length=1, max_length=MAX_PUBLIC_KEY_LEN)
    nonce: str
    signature: str
    handle: str = Field(min_length=1, max_length=HANDLE_MAX)
    display_name: str = Field(min_length=1, max_length=200)


class ChangeRole(BaseModel):
    role: str


@router.post("/v1/auth/challenge", status_code=201)
def create_challenge(
    body: ChallengeBody, request: Request, session: Session = Depends(get_session)
):
    _rate_limit(request)
    raw = generate_token()
    session.add(
        AuthChallenge(
            id=new_id("chl"),
            public_key=body.public_key,
            nonce_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(minutes=CHALLENGE_MINUTES),
        )
    )
    # The raw nonce is returned exactly once; only its hash is stored.
    return {"nonce": raw}


@router.post("/v1/auth/verify")
def verify_challenge(
    body: VerifyBody, request: Request, session: Session = Depends(get_session)
):
    _rate_limit(request)
    consume_challenge(session, body.public_key, body.nonce, body.signature)
    principal = session.scalar(
        select(Principal).where(Principal.public_key == body.public_key)
    )
    if principal is None or not principal.is_active:
        # The key is real but unknown here — enrollment goes through invites.
        raise HTTPException(403, "this key is not enrolled on this server")
    raw = _issue_token(session, principal)
    return {"token": raw, **me_payload(session, principal)}


@router.get("/v1/me")
def me(
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    return me_payload(session, principal)


@router.get("/v1/workspaces")
def list_workspaces(
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    return {"workspaces": me_payload(session, principal)["workspaces"]}


@router.post("/v1/workspaces", status_code=201)
def create_workspace(
    body: CreateWorkspace,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    # Only the server owner creates graphs here — hosting a graph IS running
    # (or renting) the server. Members join through invites.
    if not principal.is_owner:
        raise HTTPException(403, "only the server owner can create graphs here")
    workspace = Workspace(id=new_id("ws"), name=body.name.strip())
    membership = Membership(
        id=new_id("mem"),
        user_id=principal.id,
        workspace_id=workspace.id,
        role="host",
        handle=_normalize_handle(body.handle),
        display_name=body.display_name.strip(),
    )
    # Flush parents before FK'd rows: without ORM relationships SQLAlchemy
    # gives no cross-table insert ordering, and Postgres enforces the FKs
    # at flush (SQLite's default-off enforcement masked this).
    session.add(workspace)
    session.flush()
    session.add(membership)
    session.flush()
    return membership_payload(membership, workspace)


@router.post("/v1/invites", status_code=201)
def create_invite(
    body: CreateInvite,
    auth: AuthContext = Depends(require_admin),
    session: Session = Depends(get_session),
):
    if body.role not in ("member", "admin"):
        raise HTTPException(400, "role must be 'member' or 'admin'")
    if body.role == "admin" and auth.role != "host":
        raise HTTPException(403, "only the host can mint admin invites")
    raw = generate_token()
    expires = utcnow() + timedelta(days=INVITE_DAYS)
    session.add(
        Invite(
            id=new_id("inv"),
            workspace_id=auth.workspace.id,
            token_hash=hash_token(raw),
            role=body.role,
            created_by=auth.principal.id,
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
    invite = session.scalar(
        select(Invite).where(Invite.token_hash == hash_token(body.token))
    )
    now = utcnow()
    if invite is None or invite.expires_at <= now:
        raise HTTPException(400, "invite link is invalid or has expired")
    if invite.accepted_at is not None:
        raise HTTPException(400, "invite link has already been used")
    # Enrollment proves key possession — same challenge flow as sign-in.
    consume_challenge(session, body.public_key, body.nonce, body.signature)

    principal = session.scalar(
        select(Principal).where(Principal.public_key == body.public_key)
    )
    if principal is None:
        principal = Principal(id=new_id("pcp"), public_key=body.public_key)
        session.add(principal)
        session.flush()
    elif not principal.is_active:
        raise HTTPException(403, "identity disabled")

    existing = session.scalar(
        select(Membership).where(
            Membership.user_id == principal.id,
            Membership.workspace_id == invite.workspace_id,
        )
    )
    if existing is not None and existing.is_active:
        raise HTTPException(400, "you are already a member of this graph")

    handle = _normalize_handle(body.handle)
    handle_taken = session.scalar(
        select(Membership.id).where(
            Membership.workspace_id == invite.workspace_id,
            Membership.handle == handle,
        )
    )
    if handle_taken is not None and (existing is None or existing.handle != handle):
        raise HTTPException(400, f"the handle @{handle} is taken in this graph")

    workspace = session.get(Workspace, invite.workspace_id)
    if existing is not None:
        # Re-joining after removal: reactivate the old seat under the new name.
        existing.is_active = True
        existing.role = invite.role
        existing.handle = handle
        existing.display_name = body.display_name.strip()
        membership = existing
    else:
        membership = Membership(
            id=new_id("mem"),
            user_id=principal.id,
            workspace_id=workspace.id,
            role=invite.role,
            handle=handle,
            display_name=body.display_name.strip(),
        )
        session.add(membership)
    invite.accepted_at = now
    invite.accepted_by = principal.id
    session.flush()
    # A session token comes back with enrollment — joining IS signing in.
    raw = _issue_token(session, principal)
    return {"token": raw, **membership_payload(membership, workspace)}


@router.post("/v1/members/{principal_id}/role")
def change_role(
    principal_id: str,
    body: ChangeRole,
    auth: AuthContext = Depends(require_host),
    session: Session = Depends(get_session),
):
    if body.role not in ("member", "admin"):
        raise HTTPException(400, "role must be 'member' or 'admin'")
    membership = session.scalar(
        select(Membership).where(
            Membership.workspace_id == auth.workspace.id,
            Membership.user_id == principal_id,
            Membership.is_active.is_(True),
        )
    )
    if membership is None:
        raise HTTPException(404, "no active member with that id")
    if membership.role == "host":
        raise HTTPException(400, "the host role cannot be changed")
    membership.role = body.role
    return membership_payload(membership, auth.workspace)
