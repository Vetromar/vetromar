"""BYO AI providers. `vetromar.ai.get_provider` chooses the implementation;
callers only see the base interface."""

from vetromar.providers.base import (
    AgentTurn,
    AIProvider,
    Conversation,
    CredentialsRejected,
    ToolCall,
    ToolSpec,
)

__all__ = [
    "AgentTurn",
    "AIProvider",
    "Conversation",
    "CredentialsRejected",
    "ToolCall",
    "ToolSpec",
]
