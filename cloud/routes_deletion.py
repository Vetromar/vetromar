"""Workspace and account deletion — the explicit destructive path.

An admin can delete the whole workspace (wipes every server-side row, signs
all members out), and any user can delete their own account. Both re-require
the caller's password — a stolen session token alone must not be able to
destroy data. Local knowledge on members' machines is never touched."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .deps import AuthContext, current_auth, get_session, require_admin
from .models import (
    Change,
    Device,
    Invite,
    Membership,
    ResetToken,
    Token,
    User,
    Workspace,
)
from .security import verify_password

logger = logging.getLogger("cloud.deletion")

router = APIRouter()


class ConfirmPassword(BaseModel):
    password: str


def _check_password(auth: AuthContext, password: str) -> None:
    if not verify_password(auth.user.password_hash, password):
        raise HTTPException(403, "password is incorrect")


def _delete_user_rows(session: Session, user_id: str) -> None:
    session.execute(delete(Token).where(Token.user_id == user_id))
    session.execute(delete(ResetToken).where(ResetToken.user_id == user_id))
    session.execute(delete(Device).where(Device.user_id == user_id))
    session.execute(delete(Invite).where(Invite.created_by == user_id))
    session.execute(delete(User).where(User.id == user_id))


def _delete_workspace_rows(session: Session, ws: Workspace) -> None:
    """FK-safe erase of everything the workspace owns. Users whose only
    membership was this workspace are deleted too; users who also belong to
    another workspace survive (only their membership here goes)."""
    user_ids = set(
        session.scalars(
            select(Membership.user_id).where(Membership.workspace_id == ws.id)
        )
    )
    session.execute(delete(Change).where(Change.workspace_id == ws.id))
    session.execute(delete(Device).where(Device.workspace_id == ws.id))
    session.execute(delete(Invite).where(Invite.workspace_id == ws.id))
    session.execute(delete(Membership).where(Membership.workspace_id == ws.id))
    session.flush()
    for uid in user_ids:
        remaining = session.scalar(
            select(Membership.id).where(Membership.user_id == uid).limit(1)
        )
        if remaining is None:
            _delete_user_rows(session, uid)
    session.delete(ws)


@router.delete("/v1/workspaces")
def delete_workspace(
    body: ConfirmPassword,
    auth: AuthContext = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    _check_password(auth, body.password)
    ws = auth.workspace
    ws_name, ws_id = ws.name, ws.id
    _delete_workspace_rows(session, ws)
    logger.info("workspace %s (%s) deleted by %s", ws_id, ws_name, auth.user.email)
    return {"deleted": True}


@router.delete("/v1/me")
def delete_account(
    body: ConfirmPassword,
    auth: AuthContext = Depends(current_auth),
    session: Session = Depends(get_session),
) -> dict:
    _check_password(auth, body.password)
    uid = auth.user.id
    email = auth.user.email
    memberships = session.scalars(
        select(Membership).where(
            Membership.user_id == uid, Membership.is_active.is_(True)
        )
    ).all()

    solo_workspace_ids: list[str] = []
    for m in memberships:
        other_member = session.scalar(
            select(Membership.id)
            .where(
                Membership.workspace_id == m.workspace_id,
                Membership.user_id != uid,
                Membership.is_active.is_(True),
            )
            .limit(1)
        )
        if other_member is None:
            # A workspace of one: deleting the account deletes it outright.
            solo_workspace_ids.append(m.workspace_id)
            continue
        if m.role == "admin":
            other_admin = session.scalar(
                select(Membership.id)
                .where(
                    Membership.workspace_id == m.workspace_id,
                    Membership.user_id != uid,
                    Membership.role == "admin",
                    Membership.is_active.is_(True),
                )
                .limit(1)
            )
            if other_admin is None:
                raise HTTPException(
                    400,
                    "you are the only admin of a workspace with other members — "
                    "promote another admin first, or delete the workspace",
                )

    for ws_id in solo_workspace_ids:
        _delete_workspace_rows(session, session.get(Workspace, ws_id))

    # Leave every surviving workspace, then erase the user.
    session.execute(delete(Membership).where(Membership.user_id == uid))
    session.flush()
    if session.get(User, uid) is not None:
        _delete_user_rows(session, uid)
    logger.info("account %s deleted (solo workspaces: %d)", email, len(solo_workspace_ids))
    return {"deleted": True}
