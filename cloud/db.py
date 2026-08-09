"""Engine/session wiring. CLOUD_DATABASE_URL env picks the database;

default is a local SQLite file so `python -m cloud` needs zero setup.
The schema is Postgres-portable — swapping the URL is the whole migration
story for the deployment session (plus the seq row-lock note in models.py).
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_DATABASE_URL = "sqlite:///" + str(Path.home() / ".vetromar" / "cloud-dev.db")


def database_url() -> str:
    url = os.environ.get("CLOUD_DATABASE_URL") or DEFAULT_DATABASE_URL
    # Managed Postgres (Railway et al.) hands out bare postgresql:// URLs,
    # which SQLAlchemy maps to psycopg2; we ship psycopg3.
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _enforce_sqlite_fks(engine: Engine) -> Engine:
    # SQLite ships with FK enforcement OFF; Postgres enforces always. Turn
    # it on so dev/tests surface FK-ordering bugs before deploy.
    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    if url.startswith("sqlite"):
        if url.endswith(":memory:") or url == "sqlite://":
            # Shared in-memory DB across sessions/threads (tests).
            return _enforce_sqlite_fks(
                create_engine(
                    url,
                    connect_args={"check_same_thread": False},
                    poolclass=StaticPool,
                )
            )
        db_file = url.removeprefix("sqlite:///")
        Path(db_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return _enforce_sqlite_fks(
            create_engine(url, connect_args={"check_same_thread": False})
        )
    return create_engine(url)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


# Additive column migrations: create_all creates missing TABLES but never adds
# COLUMNS to existing ones — without this, the first deploy after a model gains
# a column 500s against live Postgres. All entries must be nullable (additive-
# safe on both SQLite and Postgres, idempotent, no table rewrite).
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = []


def ensure_columns(engine: Engine) -> None:
    """Run after create_all on every boot; adds any missing declared columns."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl_type in _COLUMN_MIGRATIONS:
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
