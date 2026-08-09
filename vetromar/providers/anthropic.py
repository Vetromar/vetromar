"""Native Anthropic provider.

Structured calls use the SDK's `messages.parse` with Pydantic output; the
adaptive-thinking parameter is gated per model (haiku-tier models 400 on it).
Key resolution stays in `vetromar.ai.anthropic_client` — this class only
speaks the wire format.
"""

from __future__ import annotations

from vetromar.config import model_supports_adaptive_thinking
from vetromar.errors import ConfigError
from vetromar.providers.base import (
    AgentTurn,
    AIProvider,
    Conversation,
    CredentialsRejected,
    ToolCall,
    ToolSpec,
)


class _AnthropicConversation(Conversation):
    def __init__(self, client, model: str, system: str, user: str,
                 tools: list[ToolSpec], max_tokens: int):
        self._client = client
        self._model = model
        self._system = system
        self._max_tokens = max_tokens
        self._tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        self._messages: list[dict] = [{"role": "user", "content": user}]
        # Assistant content from a step that made no tool calls, held until
        # the caller decides whether to continue: roles must alternate, and an
        # empty assistant turn can't be echoed back at all, so a follow-up
        # user message may need folding into the previous one instead.
        self._pending_assistant: list | None = None

    def step(self) -> AgentTurn:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._system,
            tools=self._tools,
            messages=self._messages,
        )
        content = list(response.content)
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in content
            if getattr(b, "type", None) == "tool_use"
        ]
        if tool_calls:
            self._messages.append({"role": "assistant", "content": content})
            self._pending_assistant = None
        else:
            self._pending_assistant = content
        return AgentTurn(tool_calls=tool_calls, ended=response.stop_reason == "end_turn")

    def add_tool_results(
        self, results: list[tuple[str, str]], trailing_text: str | None = None
    ) -> None:
        content: list[dict] = [
            {"type": "tool_result", "tool_use_id": call_id, "content": output}
            for call_id, output in results
        ]
        if trailing_text is not None:
            # Trailing text shares the user message (tool_result blocks must
            # lead it).
            content.append({"type": "text", "text": trailing_text})
        self._messages.append({"role": "user", "content": content})

    def add_user_text(self, text: str) -> None:
        pending, self._pending_assistant = self._pending_assistant, None
        if pending:
            self._messages.append({"role": "assistant", "content": pending})
            self._messages.append({"role": "user", "content": text})
            return
        # No (or an empty) assistant turn to echo back — fold into the
        # previous user message so roles keep alternating.
        prev = self._messages[-1]
        if isinstance(prev["content"], str):
            prev["content"] += "\n\n" + text
        else:
            prev["content"] = list(prev["content"]) + [{"type": "text", "text": text}]


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, config, client=None):
        self._config = config
        self.model = config.api_model
        if client is None:
            # Raises the friendly sign-in ConfigError when no access path
            # exists — provider construction is the fail-fast seam.
            from vetromar.ai import anthropic_client

            client = anthropic_client(config)
        self._client = client

    def _thinking(self) -> dict:
        return (
            {"thinking": {"type": "adaptive"}}
            if model_supports_adaptive_thinking(self.model)
            else {}
        )

    def parse_structured(self, *, system, user, schema, max_tokens=16000):
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            **self._thinking(),
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if response.parsed_output is None:
            raise RuntimeError(
                f"structured call returned no parseable output "
                f"(stop_reason={response.stop_reason!r})"
            )
        return response.parsed_output

    def start_conversation(self, *, system, user, tools, max_tokens=16000):
        return _AnthropicConversation(
            self._client, self.model, system, user, tools, max_tokens
        )

    def map_error(self, exc: Exception) -> ConfigError | None:
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError):
            return ConfigError(
                "Anthropic rejected the API key.",
                hint="Update the key in Settings, or check ANTHROPIC_API_KEY.",
            )
        return None

    def check_credentials(self) -> None:
        import anthropic

        try:
            self._client.models.list(limit=1)
        except anthropic.AuthenticationError as exc:
            raise CredentialsRejected("That key was rejected by Anthropic.") from exc
        except anthropic.APIConnectionError as exc:
            raise ConfigError(
                f"Could not reach Anthropic to validate the key ({exc}).",
                hint="Check your connection and try again.",
            ) from exc
