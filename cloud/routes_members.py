"""Member listing/removal, reset links, and device registration."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .deps import AuthContext, current_auth, get_session, require_admin
from .models import (
    RESET_TOKEN_MINUTES,
    Device,
    Membership,
    ResetToken,
    Token,
    User,
    new_id,
    utcnow,
)
from .security import generate_token, hash_token

router = APIRouter()


@router.get("/v1/members")
def list_members(
    auth: AuthContext = Depends(current_auth),
    session: Session = Depends(get_session),
):
    rows = session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == auth.workspace.id)
        .order_by(Membership.created_at)
    ).all()
    return {
        "members": [
            {
                "user_id": user.id,
                "name": user.name,
                "email": user.email,
                "role": membership.role,
                "active": membership.is_active,
            }
            for membership, user in rows
        ]
    }


@router.delete("/v1/members/{user_id}", status_code=204)
def remove_member(
    user_id: str,
    auth: AuthContext = Depends(require_admin),
    session: Session = Depends(get_session),
):
    membership = session.scalar(
        select(Membership).where(
            Membership.workspace_id == auth.workspace.id,
            Membership.user_id == user_id,
            Membership.is_active.is_(True),
        )
    )
    if membership is None:
        raise HTTPException(404, "no active member with that id")
    if membership.role == "admin":
        other_admins = session.scalar(
            select(Membership.id).where(
                Membership.workspace_id == auth.workspace.id,
                Membership.role == "admin",
                Membership.is_active.is_(True),
                Membership.user_id != user_id,
            )
        )
        if other_admins is None:
            raise HTTPException(400, "cannot remove the only admin")
    membership.is_active = False
    # Removal must cut sync access immediately — revoke every token.
    session.execute(delete(Token).where(Token.user_id == user_id))
    session.flush()


@router.post("/v1/members/{user_id}/reset-link", status_code=201)
def member_reset_link(
    user_id: str,
    auth: AuthContext = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """The server never sends email — an admin mints a reset link here and
    hands it to the member over any channel."""
    row = session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.workspace_id == auth.workspace.id,
            Membership.user_id == user_id,
            Membership.is_active.is_(True),
            User.is_active.is_(True),
        )
    ).first()
    if row is None:
        raise HTTPException(404, "no active member with that id")
    _, user = row
    raw = generate_token()
    expires = utcnow() + timedelta(minutes=RESET_TOKEN_MINUTES)
    session.add(
        ResetToken(
            id=new_id("rst"),
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=expires,
        )
    )
    # Raw token is returned exactly once; only its hash is stored.
    return {
        "token": raw,
        "url_path": f"/reset-password?token={raw}",
        "expires_at": expires.isoformat() + "Z",
        "email": user.email,
        "name": user.name,
    }


class RegisterDevice(BaseModel):
    name: str = ""


@router.put("/v1/devices/{device_id}")
def register_device(
    device_id: str,
    body: RegisterDevice,
    auth: AuthContext = Depends(current_auth),
    session: Session = Depends(get_session),
):
    device = session.scalar(select(Device).where(Device.device_id == device_id))
    if device is None:
        device = Device(
            id=new_id("dev"),
            workspace_id=auth.workspace.id,
            user_id=auth.user.id,
            device_id=device_id,
            name=body.name,
        )
        session.add(device)
    else:
        if device.workspace_id != auth.workspace.id:
            raise HTTPException(403, "device is registered to another workspace")
        if body.name:
            device.name = body.name
    device.last_seen_at = utcnow()
    return {"device_id": device_id, "name": device.name}
