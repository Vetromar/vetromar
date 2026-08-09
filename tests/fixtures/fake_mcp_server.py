"""A fake chat MCP server for tests — run over real stdio by stdio_client.

Chat-shaped and deterministic: a fixed #eng conversation (with a decision in
it, so generic-extraction-shaped content exists), a read tool with an
`after_ts` filter (the cursor mechanics), and a mutating tool the sync rails
must refuse to call. Set FAKE_CHAT_EXTRA_JSON to append messages — how tests
simulate the channel growing between syncs.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-chat")

MESSAGES = [
    {"ts": "100.1", "channel": "eng", "author": "priya", "text": "we need to pick a queue for the export jobs"},
    {"ts": "100.2", "channel": "eng", "author": "sam", "text": "redis streams is already in the stack, zero new infra"},
    {"ts": "100.3", "channel": "eng", "author": "priya", "text": "agreed, decision: export jobs go on redis streams, sam owns the rollout"},
    {"ts": "200.1", "channel": "design", "author": "mara", "text": "new onboarding flow mocks are up for review"},
]


def _messages():
    extra = os.environ.get("FAKE_CHAT_EXTRA_JSON")
    return MESSAGES + (json.loads(extra) if extra else [])


@mcp.tool()
def list_channels() -> list[str]:
    """Channels in the workspace."""
    return sorted({m["channel"] for m in _messages()})


@mcp.tool()
def list_messages(channel: str, after_ts: str = "") -> list[dict]:
    """Messages in a channel, oldest first, strictly newer than after_ts."""
    return [
        m for m in _messages()
        if m["channel"] == channel and (not after_ts or float(m["ts"]) > float(after_ts))
    ]


@mcp.tool()
def post_message(channel: str, text: str) -> dict:
    """Post a message (MUTATING — the sync agent must never call this)."""
    raise RuntimeError("mutating tool was called during sync")


if __name__ == "__main__":
    mcp.run()
