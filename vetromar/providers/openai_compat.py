"""OpenAI-compatible provider: any endpoint speaking the chat-completions
API — OpenAI, OpenRouter, Groq, Ollama (`http://localhost:11434/v1`),
LM Studio, vLLM, Gemini's compat endpoint, a LiteLLM proxy.

Two portability problems are absorbed here so callers stay provider-blind:

- structured output: server support ranges from strict json_schema to nothing,
  so a three-tier negotiation ladder (strict json_schema → json_object →
  prompted JSON) discovers the best supported mode once per provider instance;
  every tier's output is validated against the same Pydantic schema, and
  content quality downstream is protected by the evidence gate + quote
  healing regardless (the cheap-model constraint).
- tool calls: ToolSpecs translate to function tools, tool results to
  role:"tool" messages, and malformed tool arguments (weak models emit them)
  become error tool results instead of crashes.
"""

from __future__ import annotations

import copy
import json

from vetromar.errors import ConfigError
from vetromar.providers.base import (
    AgentTurn,
    AIProvider,
    Conversation,
    CredentialsRejected,
    ToolCall,
    ToolSpec,
)

# JSON Schema keywords strict mode implementations commonly reject; stripping
# them only loosens validation, and Pydantic re-validates the full schema
# client-side anyway.
_STRICT_UNSUPPORTED_KEYS = frozenset(
    {
        "default",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "discriminator",
    }
)


def strictify_schema(schema: dict) -> dict:
    """Post-process a Pydantic JSON schema for strict-mode json_schema
    response_format: every object closes additionalProperties, every property
    becomes required, unsupported keywords are stripped."""

    def walk(node):
        if isinstance(node, dict):
            for key in list(node.keys()):
                if key in _STRICT_UNSUPPORTED_KEYS:
                    del node[key]
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties", {})
                node["required"] = list(props.keys())
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    out = copy.deepcopy(schema)
    walk(out)
    return out


def _schema_prompt(user: str, schema) -> str:
    return (
        user
        + "\n\nRespond with ONLY a JSON object — no code fences, no commentary — "
        "matching this JSON Schema exactly:\n"
        + json.dumps(schema.model_json_schema())
    )


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


class _OpenAIConversation(Conversation):
    def __init__(self, provider: "OpenAICompatProvider", system: str, user: str,
                 tools: list[ToolSpec], max_tokens: int):
        self._provider = provider
        self._max_tokens = max_tokens
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        self._messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def step(self) -> AgentTurn:
        response = self._provider._create(
            messages=self._messages, max_tokens=self._max_tokens, tools=self._tools
        )
        choice = response.choices[0]
        message = choice.message
        entry: dict = {"role": "assistant", "content": message.content}
        raw_calls = list(message.tool_calls or [])
        if raw_calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in raw_calls
            ]
        self._messages.append(entry)
        tool_calls = []
        for c in raw_calls:
            try:
                args = json.loads(c.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be a JSON object")
            except ValueError:
                # Weak models emit broken argument JSON; answer with an error
                # result so the run continues instead of crashing.
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c.id,
                        "content": json.dumps(
                            {"error": "malformed tool arguments (invalid JSON) — retry the call"}
                        ),
                    }
                )
                continue
            tool_calls.append(ToolCall(id=c.id, name=c.function.name, input=args))
        return AgentTurn(tool_calls=tool_calls, ended=choice.finish_reason != "tool_calls")

    def add_tool_results(
        self, results: list[tuple[str, str]], trailing_text: str | None = None
    ) -> None:
        for call_id, output in results:
            self._messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": output}
            )
        if trailing_text is not None:
            self._messages.append({"role": "user", "content": trailing_text})

    def add_user_text(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})


class OpenAICompatProvider(AIProvider):
    name = "openai"

    def __init__(self, config, client=None):
        self._config = config
        self.model = config.api_model
        self._base_url = (config.openai_base_url or "").strip()
        if not self._base_url:
            raise ConfigError(
                "No OpenAI-compatible endpoint configured.",
                hint="Set the base URL in Settings (or VETROMAR_OPENAI_BASE_URL), e.g. "
                "https://api.openai.com/v1 or http://localhost:11434/v1 for Ollama.",
            )
        if client is not None:
            self._client = client
        else:
            import openai

            # Local servers (Ollama, LM Studio) accept any non-empty key; the
            # SDK just refuses an empty one.
            self._client = openai.OpenAI(
                base_url=self._base_url,
                api_key=config.openai_api_key or "not-needed",
                max_retries=2,
            )
        # Discovered structured-output support tier (1 strict json_schema,
        # 2 json_object, 3 prompted JSON) — negotiated once per instance.
        self._structured_tier: int | None = None
        self._max_tokens_param = "max_tokens"

    def _create(self, *, messages, max_tokens, **kwargs):
        import openai

        try:
            return self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                **{self._max_tokens_param: max_tokens},
                **kwargs,
            )
        except openai.BadRequestError as exc:
            # Newer OpenAI models reject max_tokens in favor of
            # max_completion_tokens; most compat servers only know max_tokens.
            if self._max_tokens_param == "max_tokens" and "max_completion_tokens" in str(exc):
                self._max_tokens_param = "max_completion_tokens"
                return self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                    **kwargs,
                )
            raise

    def _structured_call(self, tier: int, system: str, user: str, schema, max_tokens: int) -> str:
        messages = [{"role": "system", "content": system}]
        kwargs: dict = {}
        if tier == 1:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": strictify_schema(schema.model_json_schema()),
                },
            }
            messages.append({"role": "user", "content": user})
        elif tier == 2:
            kwargs["response_format"] = {"type": "json_object"}
            messages.append({"role": "user", "content": _schema_prompt(user, schema)})
        else:
            messages.append({"role": "user", "content": _schema_prompt(user, schema)})
        response = self._create(messages=messages, max_tokens=max_tokens, **kwargs)
        choice = response.choices[0]
        if not choice.message.content:
            raise RuntimeError(
                f"structured call returned no content "
                f"(finish_reason={choice.finish_reason!r})"
            )
        return choice.message.content

    def parse_structured(self, *, system, user, schema, max_tokens=16000):
        import openai

        from pydantic import ValidationError

        tiers = [self._structured_tier] if self._structured_tier is not None else [1, 2, 3]
        last_exc: Exception | None = None
        for tier in tiers:
            try:
                text = self._structured_call(tier, system, user, schema, max_tokens)
            except openai.BadRequestError as exc:
                # The endpoint rejected this response_format tier — fall to
                # the next. A genuine bad request surfaces from tier 3, which
                # sends no response_format at all.
                last_exc = exc
                continue
            self._structured_tier = tier
            try:
                return schema.model_validate_json(_strip_fences(text))
            except ValidationError as exc:
                raise RuntimeError(
                    f"structured output failed schema validation: {exc}"
                ) from exc
        assert last_exc is not None
        raise last_exc

    def start_conversation(self, *, system, user, tools, max_tokens=16000):
        return _OpenAIConversation(self, system, user, tools, max_tokens)

    def map_error(self, exc: Exception) -> ConfigError | None:
        import openai

        if isinstance(exc, openai.AuthenticationError):
            return ConfigError(
                f"The endpoint at {self._base_url} rejected the API key.",
                hint="Check the key in Settings (or OPENAI_API_KEY).",
            )
        if isinstance(exc, openai.NotFoundError):
            return ConfigError(
                f"The endpoint at {self._base_url} does not recognize model "
                f"{self.model!r}.",
                hint="Set the model in Settings (or VETROMAR_API_MODEL) to one this "
                "endpoint serves.",
            )
        if isinstance(exc, openai.APIConnectionError):
            return ConfigError(
                f"Could not reach the OpenAI-compatible endpoint at {self._base_url}.",
                hint="Check the base URL and that the server is running.",
            )
        return None

    def check_credentials(self) -> None:
        import openai

        try:
            self._client.models.list()
        except openai.AuthenticationError as exc:
            raise CredentialsRejected(
                f"The endpoint at {self._base_url} rejected the API key."
            ) from exc
        except openai.NotFoundError:
            # Many proxies (LiteLLM, vLLM configs) don't serve /models —
            # configured but unverifiable is a pass, not a failure.
            return
        except openai.APIConnectionError as exc:
            raise ConfigError(
                f"Could not reach the OpenAI-compatible endpoint at {self._base_url} ({exc}).",
                hint="Check the base URL and that the server is running.",
            ) from exc
