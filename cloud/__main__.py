"""Run the cloud service locally: `python -m cloud [--port 8787]`.

Operator subcommands run on the server box against the same database:

    python -m cloud reset-link you@example.com   # mint a password-reset link
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from .app import create_app
from .db import database_url, ensure_columns, make_engine, make_sessionmaker


def _cmd_reset_link(email: str) -> int:
    from datetime import timedelta

    from sqlalchemy import select

    from .emailer import public_url
    from .models import RESET_TOKEN_MINUTES, Base, ResetToken, User, new_id, utcnow
    from .security import generate_token, hash_token

    engine = make_engine()
    Base.metadata.create_all(engine)
    ensure_columns(engine)
    email = email.strip().lower()
    with make_sessionmaker(engine)() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None or not user.is_active:
            print(f"no active account for {email}", file=sys.stderr)
            return 1
        raw = generate_token()
        session.add(
            ResetToken(
                id=new_id("rst"),
                user_id=user.id,
                token_hash=hash_token(raw),
                expires_at=utcnow() + timedelta(minutes=RESET_TOKEN_MINUTES),
            )
        )
        session.commit()
    # Bare link on stdout so it can be piped; the caveat goes to stderr.
    print(f"{public_url()}/reset-password?token={raw}")
    print(
        f"Single use, expires in {RESET_TOKEN_MINUTES} minutes.",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vetromar workspace server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    sub = parser.add_subparsers(dest="cmd")
    reset = sub.add_parser(
        "reset-link", help="mint a one-time password-reset link for an account"
    )
    reset.add_argument("email")
    args = parser.parse_args(argv)

    if args.cmd == "reset-link":
        return _cmd_reset_link(args.email)

    print(f"vetromar-cloud: db={database_url()}")
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
