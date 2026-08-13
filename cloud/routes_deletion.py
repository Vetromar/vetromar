"""Workspace and identity deletion — the explicit destructive path.

The host can delete a whole graph (wipes every server-side row, signs all
members out), and any principal can delete their identity here. Both
re-require a fresh signed challenge — a stolen session token alone must not
be able to destroy data. Local knowledge on members' machines is never
touched."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .deps import AuthContext, current_principal, get_session, require_host
from .models import (
    AuthChallenge,
    Change,
    Device,
    Invite,
    Membership,
    Principal,
    Token,
    Workspace,
)

logger = logging.getLogger("cloud.deletion")

router = APIRouter()


class SignedProof(BaseModel):
    """A freshly signed challenge nonce — the keypair era's 'retype your
    password'. Mint via /v1/auth/challenge, sign, send both halves here."""

    nonce: str
    signature: str


def _check_proof(session: Session, principal: Principal, proof: SignedProof) -> None:
    from .routes_auth import consume_challenge

    consume_challenge(session, principal.public_key, proof.nonce, proof.signature)


def _delete_principal_rows(session: Session, principal_id: str) -> None:
    principal = session.get(Principal, principal_id)
    session.execute(delete(Token).where(Token.user_id == principal_id))
    session.execute(delete(Device).where(Device.user_id == principal_id))
    session.execute(delete(Invite).where(Invite.created_by == principal_id))
    if principal is not None:
        session.execute(
            delete(AuthChallenge).where(AuthChallenge.public_key == principal.public_key)
        )
    session.execute(delete(Principal).where(Principal.id == principal_id))


def _delete_workspace_rows(session: Session, ws: Workspace) -> None:
    """FK-safe erase of everything the workspace owns. Principals whose only
    membership was this workspace are deleted too; principals who also belong
    to another workspace survive (only their membership here goes)."""
    principal_ids = set(
        session.scalars(
            select(Membership.user_id).where(Membership.workspace_id == ws.id)
        )
    )
    session.execute(delete(Change).where(Change.workspace_id == ws.id))
    session.execute(delete(Device).where(Device.workspace_id == ws.id))
    session.execute(delete(Invite).where(Invite.workspace_id == ws.id))
    session.execute(delete(Membership).where(Membership.workspace_id == ws.id))
    session.flush()
    for pid in principal_ids:
        remaining = session.scalar(
            select(Membership.id).where(Membership.user_id == pid).limit(1)
        )
        principal = session.get(Principal, pid)
        # The server owner's identity outlives any one workspace — it's the
        # bootstrap credential for creating the next one.
        if remaining is None and principal is not None and not principal.is_owner:
            _delete_principal_rows(session, pid)
    session.delete(ws)


@router.delete("/v1/workspaces")
def delete_workspace(
    body: SignedProof,
    auth: AuthContext = Depends(require_host),
    session: Session = Depends(get_session),
) -> dict:
    _check_proof(session, auth.principal, body)
    ws = auth.workspace
    ws_name, ws_id = ws.name, ws.id
    _delete_workspace_rows(session, ws)
    logger.info(
        "workspace %s (%s) deleted by host @%s", ws_id, ws_name, auth.membership.handle
    )
    return {"deleted": True}


@router.delete("/v1/me")
def delete_identity(
    body: SignedProof,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> dict:
    _check_proof(session, principal, body)
    pid = principal.id
    memberships = session.scalars(
        select(Membership).where(
            Membership.user_id == pid, Membership.is_active.is_(True)
        )
    ).all()

    solo_workspace_ids: list[str] = []
    for m in memberships:
        other_member = session.scalar(
            select(Membership.id)
            .where(
                Membership.workspace_id == m.workspace_id,
                Membership.user_id != pid,
                Membership.is_active.is_(True),
            )
            .limit(1)
        )
        if other_member is None:
            # A graph of one: deleting the identity deletes it outright.
            solo_workspace_ids.append(m.workspace_id)
            continue
        if m.role == "host":
            raise HTTPException(
                400,
                "you host a graph that still has members — delete the graph "
                "first, or wait for the members to leave",
            )
        if m.role == "admin":
            other_admin = session.scalar(
                select(Membership.id)
                .where(
                    Membership.workspace_id == m.workspace_id,
                    Membership.user_id != pid,
                    Membership.role.in_(("host", "admin")),
                    Membership.is_active.is_(True),
                )
                .limit(1)
            )
            if other_admin is None:
                raise HTTPException(
                    400,
                    "you are the only admin of a graph with other members — "
                    "ask the host to promote someone first",
                )

    for ws_id in solo_workspace_ids:
        _delete_workspace_rows(session, session.get(Workspace, ws_id))

    # Leave every surviving workspace, then erase the identity.
    session.execute(delete(Membership).where(Membership.user_id == pid))
    session.flush()
    if session.get(Principal, pid) is not None:
        _delete_principal_rows(session, pid)
    logger.info("identity %s deleted (solo graphs: %d)", pid, len(solo_workspace_ids))
    return {"deleted": True}
