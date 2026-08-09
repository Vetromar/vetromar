"""SQLAlchemy models for the cloud service.

Postgres-portable types only (String/Text/Integer/DateTime/Boolean); local
dev and tests run on SQLite via CLOUD_DATABASE_URL. Datetimes are stored
naive-UTC so both engines behave identically.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TOKEN_DAYS = 30
INVITE_DAYS = 14
RESET_TOKEN_MINUTES = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workspaces.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(10))  # 'admin' | 'member'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workspaces.id"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(10), default="member")
    created_by: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ResetToken(Base):
    """Password-reset links: single-use, short-lived, stored hashed (same
    token discipline as sessions/invites)."""

    __tablename__ = "reset_tokens"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workspaces.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id"))
    device_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Change(Base):
    """The per-workspace replication log. Log-only in v0 — this IS the

    workspace knowledge state; devices materialize it locally. `seq` is
    assigned inside the push transaction; the push handler locks the
    workspace row (SELECT ... FOR UPDATE — a no-op on SQLite's dialect)
    before computing MAX(seq), so concurrent Postgres pushes serialize.
    """

    __tablename__ = "changes"
    __table_args__ = (
        UniqueConstraint("workspace_id", "seq"),
        UniqueConstraint("workspace_id", "change_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workspaces.id"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    change_id: Mapped[str] = mapped_column(String(40))
    table_name: Mapped[str] = mapped_column(String(20))
    op: Mapped[str] = mapped_column(String(10))
    row_id: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    origin_device_id: Mapped[str] = mapped_column(String(80))
    origin_user_id: Mapped[str] = mapped_column(String(40))
    recorded_at: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


