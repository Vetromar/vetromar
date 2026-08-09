"""The replication wire format, shared by client and server.

One ChangeRecord per local store write. `payload` is the full model JSON
(`model_dump_json`) of the row after the write — the store's own canonical
serialization, so apply is just `model_validate` + the idempotency policy.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel

ChangeTable = Literal["episodes", "units", "entities", "edges"]
ChangeOp = Literal["insert", "update"]


def new_change_id() -> str:
    return f"chg_{uuid.uuid4().hex[:12]}"


class ChangeRecord(BaseModel):
    """A single replicated write, as recorded in the origin device's outbox."""

    change_id: str
    table: ChangeTable
    op: ChangeOp
    row_id: str
    payload: dict
    recorded_at: str


class ChangeEnvelope(ChangeRecord):
    """A change as it comes back from the server log."""

    seq: int
    origin_device_id: str
    origin_user_id: str
    received_at: str
