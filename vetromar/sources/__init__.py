"""Vetromar as MCP *client* — pulling knowledge out of the customer's stack.

This package is the client half of the MCP story (`vetromar/mcp_server/` is
the server half — the customer's agent querying US). The fence, restated for
this side: NO per-source code EVER. MCP is the sole adapter — Vetromar speaks
the MCP protocol and its spec-standard OAuth, never a source's native API,
and the sync agent (an LLM loop) absorbs every source's tool-shape
differences so the engine only ever sees (raw text, external_id, evidence).
"""

from vetromar.sources.deliver import DeliveryResult, deliver_episode

__all__ = ["DeliveryResult", "deliver_episode"]
