"""Opening MCP client sessions against a configured source.

Transport-generic: remote sources over streamable HTTP (with the generic
OAuth provider), local ones over stdio. Connection problems surface as
ConfigError (rendered traceback-free by the CLI) — a dead source is a setup
problem the user can fix, not a crash.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool

from vetromar.errors import ConfigError
from vetromar.sources.auth import build_oauth_provider
from vetromar.sources.registry import SourceConfig


def _unwrap(exc: BaseException) -> BaseException:
    """anyio task groups wrap everything in ExceptionGroup; a single-child
    group is just noise around the real error."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


@asynccontextmanager
async def open_session(
    source: SourceConfig, cancel_event: threading.Event | None = None
) -> AsyncIterator[ClientSession]:
    """Connect to a source's MCP server and yield an initialized session.
    Setup failures become ConfigError; errors raised by the caller's body
    propagate as themselves. `cancel_event` aborts a pending OAuth consent
    wait (remote sources only)."""
    connected = False
    try:
        if source.url:
            auth = build_oauth_provider(source.name, source.url, cancel_event=cancel_event)
            async with streamablehttp_client(source.url, auth=auth) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    connected = True
                    yield session
        else:
            params = StdioServerParameters(
                command=source.command,
                args=source.args,
                env={**os.environ, **source.env},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    connected = True
                    yield session
    except ConfigError:
        raise
    except BaseExceptionGroup as group:
        cause = _unwrap(group)
        if connected or isinstance(cause, ConfigError):
            raise cause from None
        raise ConfigError(
            f"Could not connect to source '{source.name}': {cause}",
            hint="`vetromar sources test "
            f"{source.name}` checks the connection; `vetromar connect` re-authorizes it.",
        ) from cause
    except Exception as exc:  # noqa: BLE001 — transport/auth failures become setup errors
        if connected:
            raise
        raise ConfigError(
            f"Could not connect to source '{source.name}': {exc}",
            hint="`vetromar sources test "
            f"{source.name}` checks the connection; `vetromar connect` re-authorizes it.",
        ) from exc


async def discover_tools(
    source: SourceConfig, cancel_event: threading.Event | None = None
) -> list[Tool]:
    async with open_session(source, cancel_event=cancel_event) as session:
        result = await session.list_tools()
        return result.tools


def test_source(
    source: SourceConfig, cancel_event: threading.Event | None = None
) -> list[str]:
    """Connect + list tools; the CLI's `sources test`. Returns tool names.
    `cancel_event` (set from another thread) aborts a pending OAuth consent."""
    tools = anyio.run(discover_tools, source, cancel_event)
    return [t.name for t in tools]
