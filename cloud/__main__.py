"""Run the graph host server locally: `python -m cloud [--port 8787]`.

Operator subcommands run on the server box against the same database:

    python -m cloud set-owner <public_key>   # enroll the server owner

The owner is the one principal allowed to create graphs on this server —
on a VPS you mint it once with your key from the app's Settings → Identity
card (or set CLOUD_OWNER_PUBLIC_KEY before first boot).
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from .app import create_app
from .db import database_url, ensure_columns, make_engine, make_sessionmaker


def set_owner(engine, public_key: str) -> str:
    """Enroll (or promote) `public_key` as the server owner. Returns the
    principal id. Shared by the CLI below, the CLOUD_OWNER_PUBLIC_KEY boot
    path, and the desktop app's embedded host bootstrap."""
    from sqlalchemy import select

    from .models import Base, Principal, new_id

    Base.metadata.create_all(engine)
    ensure_columns(engine)
    public_key = public_key.strip()
    if not public_key:
        raise ValueError("public key must not be empty")
    with make_sessionmaker(engine)() as session:
        principal = session.scalar(
            select(Principal).where(Principal.public_key == public_key)
        )
        if principal is None:
            principal = Principal(
                id=new_id("pcp"), public_key=public_key, is_owner=True
            )
            session.add(principal)
        else:
            principal.is_owner = True
            principal.is_active = True
        session.commit()
        return principal.id


def _cmd_set_owner(public_key: str) -> int:
    try:
        pid = set_owner(make_engine(), public_key)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(pid)
    print("This key can now create graphs on this server.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vetromar graph host server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    sub = parser.add_subparsers(dest="cmd")
    owner = sub.add_parser(
        "set-owner", help="enroll a public key as the server owner"
    )
    owner.add_argument("public_key")
    args = parser.parse_args(argv)

    if args.cmd == "set-owner":
        return _cmd_set_owner(args.public_key)

    # Container deploys can seed the owner without a shell: set the env var
    # and the key is enrolled at boot (idempotent).
    boot_owner = os.environ.get("CLOUD_OWNER_PUBLIC_KEY")
    if boot_owner:
        set_owner(make_engine(), boot_owner)

    print(f"vetromar-cloud: db={database_url()}")
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
