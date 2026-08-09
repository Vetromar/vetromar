"""Local HTTP API behind the desktop UI. Wraps the existing engine; adds no
capture/extraction logic. Requires the `ui` extra (fastapi/uvicorn)."""

from vetromar.ui_server.app import create_app, run_server

__all__ = ["create_app", "run_server"]
