"""Run the cloud service locally: `python -m cloud [--port 8787]`."""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .db import database_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Vetromar Cloud service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    print(f"vetromar-cloud: db={database_url()}")
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
