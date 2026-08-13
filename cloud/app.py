"""FastAPI app factory for the workspace server."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.engine import Engine

from .db import ensure_columns, make_engine, make_sessionmaker
from .models import Base

_STATIC = Path(__file__).parent / "static"


def create_app(engine: Engine | None = None) -> FastAPI:
    engine = engine or make_engine()
    # No alembic in v0: create_all makes missing tables; ensure_columns adds
    # any missing declared columns to existing ones (additive only).
    Base.metadata.create_all(engine)
    ensure_columns(engine)

    app = FastAPI(title="Vetromar Cloud", version="0.1.0")
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    origins = [
        o.strip()
        for o in os.environ.get("CLOUD_CORS_ORIGINS", "*").split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from .routes_auth import router as auth_router
    from .routes_deletion import router as deletion_router
    from .routes_members import router as members_router
    from .routes_sync import router as sync_router

    app.include_router(auth_router)
    app.include_router(members_router)
    app.include_router(sync_router)
    app.include_router(deletion_router)

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    # The one page the server serves itself: an invite link opened in a
    # browser explains that joining happens in the app (enrollment needs the
    # local keypair, so there is nothing a web form could do).
    @app.get("/invite-accept", include_in_schema=False)
    def invite_accept_page():
        return FileResponse(_STATIC / "invite-accept.html")

    return app
