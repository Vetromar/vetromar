"""Push/pull on the per-workspace replication log."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vetromar.workspace.wire import ChangeRecord

from .deps import AuthContext, current_auth, get_session
from .models import Change, Device, Workspace, new_id, utcnow

router = APIRouter()

MAX_PULL_LIMIT = 500


class PushBody(BaseModel):
    device_id: str
    changes: list[ChangeRecord]


def _touch_device(session: Session, auth: AuthContext, device_id: str) -> None:
    device = session.scalar(
        select(Device).where(
            Device.workspace_id == auth.workspace.id, Device.device_id == device_id
        )
    )
    if device is None:
        session.add(
            Device(
                id=new_id("dev"),
                workspace_id=auth.workspace.id,
                user_id=auth.principal.id,
                device_id=device_id,
            )
        )
    else:
        device.last_seen_at = utcnow()


@router.post("/v1/sync/push")
def push(
    body: PushBody,
    auth: AuthContext = Depends(current_auth),
    session: Session = Depends(get_session),
):
    _touch_device(session, auth, body.device_id)
    # Serialize concurrent pushes to one workspace: under Postgres two
    # transactions computing MAX(seq) together would collide on the
    # (workspace_id, seq) unique constraint. SQLite's dialect omits
    # FOR UPDATE (single writer already serializes).
    session.execute(
        select(Workspace.id).where(Workspace.id == auth.workspace.id).with_for_update()
    )
    seen = set(
        session.scalars(
            select(Change.change_id).where(
                Change.workspace_id == auth.workspace.id,
                Change.change_id.in_([c.change_id for c in body.changes]),
            )
        )
    )
    last_seq = session.scalar(
        select(func.max(Change.seq)).where(Change.workspace_id == auth.workspace.id)
    ) or 0
    accepted = 0
    duplicates = 0
    for change in body.changes:
        if change.change_id in seen:
            duplicates += 1
            continue
        seen.add(change.change_id)
        last_seq += 1
        session.add(
            Change(
                workspace_id=auth.workspace.id,
                seq=last_seq,
                change_id=change.change_id,
                table_name=change.table,
                op=change.op,
                row_id=change.row_id,
                payload=json.dumps(change.payload),
                origin_device_id=body.device_id,
                origin_user_id=auth.principal.id,
                recorded_at=change.recorded_at,
            )
        )
        accepted += 1
    return {"accepted": accepted, "duplicates": duplicates, "last_seq": last_seq}


@router.get("/v1/sync/pull")
def pull(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=MAX_PULL_LIMIT, ge=1, le=MAX_PULL_LIMIT),
    auth: AuthContext = Depends(current_auth),
    session: Session = Depends(get_session),
):
    rows = session.scalars(
        select(Change)
        .where(Change.workspace_id == auth.workspace.id, Change.seq > since)
        .order_by(Change.seq)
        .limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    changes = [
        {
            "change_id": row.change_id,
            "table": row.table_name,
            "op": row.op,
            "row_id": row.row_id,
            "payload": json.loads(row.payload),
            "recorded_at": row.recorded_at,
            "seq": row.seq,
            "origin_device_id": row.origin_device_id,
            "origin_user_id": row.origin_user_id,
            "received_at": row.received_at.isoformat() + "Z",
        }
        for row in rows
    ]
    next_since = rows[-1].seq if rows else since
    return {"changes": changes, "next_since": next_since, "has_more": has_more}
