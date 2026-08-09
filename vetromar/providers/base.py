"""Provider-neutral AI interface: structured parsing + tool-driving
conversations.

Two implementations: native Anthropic (anthropic.py) and any OpenAI-compatible
endpoint (openai_compat.py). Everything above this seam — extraction, linking,
the sync agent — is provider-blind; `vetromar.ai.get_provider` is the one
place an implementation is chosen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from vetromar.errors import ConfigError

T = TypeVar("T", bound=BaseModel)


class CredentialsRejected(Exception):
    """The provider's live auth check rejected the configured credentials."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class AgentTurn:
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The model finished its turn (vs. pausing for tool results).
    ended: bool = False


class Conversation(ABC):
    """A tool-driving exchange whose transcript the PROVIDER owns.

    Wire formats differ enough to matter — Anthropic alternates roles and
    leads user messages with tool_result blocks; OpenAI appends role:"tool"
    messages — so the transcript mechanics live behind this interface and the
    agent loop above stays pure logic."""

    @abstractmethod
    def step(self) -> AgentTurn:
        """One model call over the current transcript."""

    @abstractmethod
    def add_tool_results(
        self, results: list[tuple[str, str]], trailing_text: str | None = None
    ) -> None:
        """Feed back (tool_call_id, output) pairs for the last step's calls,
        optionally followed by user text (e.g. a completeness nudge)."""

    @abstractmethod
    def add_user_text(self, text: str) -> None:
        """Append user text (a nudge after a turn that made no tool calls)."""


class AIProvider(ABC):
    name: str = "?"

    @abstractmethod
    def parse_structured(
        self, *, system: str, user: str, schema: type[T], max_tokens: int = 16000
    ) -> T:
        """One-shot structured call: a validated `schema` instance, or raises
        (RuntimeError on unparseable output; SDK errors propagate for
        map_error to translate)."""

    @abstractmethod
    def start_conversation(
        self, *, system: str, user: str, tools: list[ToolSpec], max_tokens: int = 16000
    ) -> Conversation:
        """Open a tool-driving conversation seeded with one user message."""

    @abstractmethod
    def map_error(self, exc: Exception) -> ConfigError | None:
        """Translate a provider SDK error into a friendly ConfigError, or None
        when it isn't ours to translate (caller re-raises the original)."""

    @abstractmethod
    def check_credentials(self) -> None:
        """Live auth probe. Raises CredentialsRejected on a bad key,
        ConfigError on an unreachable/misconfigured endpoint; returns when the
        credentials work (or the endpoint offers no way to disprove them)."""
