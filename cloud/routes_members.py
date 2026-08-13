"""Member listing/removal and device registration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .deps import AuthContext, current_auth, get_session, require_admin
from .models import (
    Device,
    Membership,
    Principal,
    Token,
    new_id,
    utcnow,
)

router = APIRouter()


@router.get("/v1/members")
def list_members(
    auth: AuthContext = Depends(current_auth),
    session: Session = Depends(get_session),
):
    rows = session.execute(
        select(Membership, Principal)
        .join(Principal, Principal.id == Membership.user_id)
        .where(Membership.workspace_id == auth.workspace.id)
        .order_by(Membership.created_at)
    ).all()
    return {
        "members": [
            {
                "principal_id": principal.id,
                "public_key": principal.public_key,
                "handle": membership.handle,
                "display_name": membership.display_name,
                "role": membership.role,
                "active": membership.is_active,
            }
            for membership, principal in rows
        ]
    }


@router.delete("/v1/members/{principal_id}", status_code=204)
def remove_member(
    principal_id: str,
    auth: AuthContext = Depends(require_admin),
    session: Session = Depends(get_session),
):
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
        raise HTTPException(400, "the host cannot be removed from their own graph")
    if membership.role == "admin" and auth.role != "host":
        raise HTTPException(403, "only the host can remove an admin")
    membership.is_active = False
    # Removal must cut sync access immediately. Tokens are per-principal
    # (server-wide), so this also signs them out of their OTHER graphs on
    # this server — they re-auth via challenge and keep those seats.
    # Accepted v1 cost for the guarantee that removal is instant.
    session.execute(delete(Token).where(Token.user_id == principal_id))
    session.flush()


class RegisterDevice(BaseModel):
    name: str = ""


@router.put("/v1/devices/{device_id}")
def register_device(
    device_id: str,
    body: RegisterDevice,
    auth: AuthContext = Depends(current_auth),
    session: Session = Depends(get_session),
):
    # Devices are per (workspace, device): one installation syncing N graphs
    # on this server registers N rows.
    device = session.scalar(
        select(Device).where(
            Device.workspace_id == auth.workspace.id, Device.device_id == device_id
        )
    )
    if device is None:
        device = Device(
            id=new_id("dev"),
            workspace_id=auth.workspace.id,
            user_id=auth.principal.id,
            device_id=device_id,
            name=body.name,
        )
        session.add(device)
    elif body.name:
        device.name = body.name
    device.last_seen_at = utcnow()
    return {"device_id": device_id, "name": device.name}
