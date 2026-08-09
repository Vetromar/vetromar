"""Workspace sync client: accounts session + log replication engine.

This package is bundled into the desktop sidecar — keep dependencies to
pydantic/httpx/stdlib. The cloud service (`cloud/`) imports the wire model
from here; nothing in `vetromar` may ever import `cloud`.
"""
