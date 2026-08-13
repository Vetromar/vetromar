"""Workspace sync client: challenge auth + log replication engine.

This package is bundled into the desktop sidecar — keep dependencies to
pydantic/httpx/stdlib. The graph host (`cloud/`) imports the wire model
from here; `vetromar` imports `cloud` only via `vetromar/hosting/` (lazy).
"""
