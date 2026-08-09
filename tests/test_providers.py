"""The BYO provider seam: OpenAI-compatible wire behavior (against the real
SDK + a scripted HTTP server), the structured-output negotiation ladder,
tool-loop translation, error mapping, and provider selection in vetromar.ai."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from fake_openai_server import FakeOpenAIServer, completion  # noqa: E402

from vetromar import ai  # noqa: E402
from vetromar.config import Config  # noqa: E402
from vetromar.errors import ConfigError  # noqa: E402
from vetromar.providers import CredentialsRejected, ToolSpec  # noqa: E402
from vetromar.providers.anthropic import AnthropicProvider  # noqa: E402
from vetromar.providers.openai_compat import (  # noqa: E402
    OpenAICompatProvider,
    strictify_schema,
)


class Finding(BaseModel):
    title: str
    tags: list[str]


class FindingList(BaseModel):
    findings: list[Finding]


PAYLOAD = {"findings": [{"title": "t1", "tags": ["a", "b"]}]}


def _config(base_url, key="test-key", model="fake-model", **overrides):
    defaults = dict(
        ai_provider="openai",
        openai_base_url=base_url,
        openai_api_key=key,
        api_model=model,
        api_key=None,
        cloud_token=None,
        deepgram_api_key=None,
    )
    return Config(**{**defaults, **overrides})


def _parse(provider):
    return provider.parse_structured(system="sys", user="extract", schema=FindingList)


# -- provider selection (vetromar.ai) -----------------------------------------


def test_get_provider_selects_by_config():
    p = ai.get_provider(_config("http://127.0.0.1:1/v1"))
    assert isinstance(p, OpenAICompatProvider)
    q = ai.get_provider(
        Config(ai_provider="anthropic", api_key="sk-ant-x", cloud_token=None)
    )
    assert isinstance(q, AnthropicProvider)


def test_openai_provider_without_base_url_is_config_error():
    with pytest.raises(ConfigError, match="No OpenAI-compatible endpoint"):
        ai.get_provider(_config(None))


def test_ai_available_openai_keyed_on_base_url():
    assert ai.ai_available(_config("http://127.0.0.1:1/v1", key=None)) is True
    # An Anthropic key does not make the *selected* openai provider available.
    assert ai.ai_available(_config(None, api_key="sk-ant-x")) is False


# -- structured output: the negotiation ladder --------------------------------


def test_strict_json_schema_tier():
    with FakeOpenAIServer([completion(content=json.dumps(PAYLOAD))]) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        result = _parse(provider)
        assert result.findings[0].title == "t1"
        assert provider._structured_tier == 1
        fmt = srv.requests[0]["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        schema = fmt["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"].keys())


def test_ladder_falls_to_json_object_and_sticks():
    script = [completion(content=json.dumps(PAYLOAD))] * 2
    with FakeOpenAIServer(script, reject_json_schema=True) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        result = _parse(provider)
        assert result.findings[0].tags == ["a", "b"]
        assert provider._structured_tier == 2
        assert srv.requests[0]["response_format"]["type"] == "json_schema"
        assert srv.requests[1]["response_format"]["type"] == "json_object"
        # tier 2 embeds the schema in the prompt
        assert "JSON Schema" in srv.requests[1]["messages"][-1]["content"]
        # a second call goes straight to the discovered tier
        _parse(provider)
        assert len(srv.requests) == 3
        assert srv.requests[2]["response_format"]["type"] == "json_object"


def test_ladder_falls_to_prompted_json_and_strips_fences():
    fenced = "```json\n" + json.dumps(PAYLOAD) + "\n```"
    with FakeOpenAIServer(
        [completion(content=fenced)], reject_json_schema=True, reject_json_object=True
    ) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        result = _parse(provider)
        assert result.findings[0].title == "t1"
        assert provider._structured_tier == 3
        assert "response_format" not in srv.requests[-1]
        assert "JSON Schema" in srv.requests[-1]["messages"][-1]["content"]


def test_invalid_structured_output_fails_loud():
    with FakeOpenAIServer([completion(content='{"nope": 1}')]) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        with pytest.raises(RuntimeError, match="failed schema validation"):
            _parse(provider)


def test_max_tokens_param_negotiation():
    with FakeOpenAIServer(
        [completion(content=json.dumps(PAYLOAD))], reject_max_tokens=True
    ) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        result = _parse(provider)
        assert result.findings
        assert provider._max_tokens_param == "max_completion_tokens"
        assert "max_tokens" in srv.requests[0]
        assert "max_completion_tokens" in srv.requests[-1]


def test_strictify_the_real_extraction_schema():
    # The schema that actually ships: nested models, unions, $defs.
    from vetromar.extraction.generic import GenericExtractionResult

    schema = strictify_schema(GenericExtractionResult.model_json_schema())

    def walk(node):
        if isinstance(node, dict):
            assert "default" not in node and "discriminator" not in node
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}).keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)


# -- tool-driving conversations -----------------------------------------------


def _tool():
    return ToolSpec(
        name="list_channels",
        description="List channels",
        input_schema={"type": "object", "properties": {}},
    )


def test_tool_round_trip():
    script = [
        completion(tool_calls=[("call_1", "list_channels", "{}")]),
        completion(content="all done", finish_reason="stop"),
    ]
    with FakeOpenAIServer(script) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        conv = provider.start_conversation(system="sys", user="go", tools=[_tool()])
        turn = conv.step()
        assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [
            ("call_1", "list_channels", {})
        ]
        assert turn.ended is False
        conv.add_tool_results([("call_1", '["eng"]')])
        turn2 = conv.step()
        assert turn2.tool_calls == [] and turn2.ended is True
        # wire shape: function tools out, role:"tool" results back
        sent_tools = srv.requests[0]["tools"]
        assert sent_tools[0] == {
            "type": "function",
            "function": {
                "name": "list_channels",
                "description": "List channels",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        roles = [m["role"] for m in srv.requests[1]["messages"]]
        assert roles == ["system", "user", "assistant", "tool"]
        tool_msg = srv.requests[1]["messages"][3]
        assert tool_msg["tool_call_id"] == "call_1" and tool_msg["content"] == '["eng"]'


def test_malformed_tool_arguments_become_error_results():
    script = [
        completion(tool_calls=[("call_bad", "list_channels", "{not json")]),
        completion(content="ok", finish_reason="stop"),
    ]
    with FakeOpenAIServer(script) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        conv = provider.start_conversation(system="sys", user="go", tools=[_tool()])
        turn = conv.step()
        assert turn.tool_calls == []  # nothing dispatchable, but no crash
        conv.add_user_text("continue")
        conv.step()
        messages = srv.requests[1]["messages"]
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert tool_msgs[0]["tool_call_id"] == "call_bad"
        assert "malformed" in tool_msgs[0]["content"]


def test_tool_results_with_trailing_text_share_the_turn():
    script = [
        completion(tool_calls=[("c1", "list_channels", "{}")], finish_reason="stop"),
        completion(content="done", finish_reason="stop"),
    ]
    with FakeOpenAIServer(script) as srv:
        provider = OpenAICompatProvider(_config(srv.base_url))
        conv = provider.start_conversation(system="sys", user="go", tools=[_tool()])
        turn = conv.step()
        assert turn.ended is True  # finish_reason != tool_calls
        conv.add_tool_results([("c1", "[]")], trailing_text="keep going")
        conv.step()
        roles = [m["role"] for m in srv.requests[1]["messages"]]
        assert roles == ["system", "user", "assistant", "tool", "user"]
        assert srv.requests[1]["messages"][-1]["content"] == "keep going"


# -- error mapping + credentials ----------------------------------------------


def test_auth_error_maps_to_rejected_key():
    openai = pytest.importorskip("openai")
    with FakeOpenAIServer(require_key="right-key") as srv:
        provider = OpenAICompatProvider(_config(srv.base_url, key="wrong-key"))
        with pytest.raises(openai.AuthenticationError) as exc:
            _parse(provider)
        mapped = provider.map_error(exc.value)
        assert "rejected the API key" in mapped.message


def test_connection_error_maps_to_unreachable():
    openai = pytest.importorskip("openai")
    provider = OpenAICompatProvider(_config("http://127.0.0.1:1/v1"))
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:1/v1"))
    mapped = provider.map_error(exc)
    assert "Could not reach" in mapped.message


def test_check_credentials_accepts_and_rejects():
    with FakeOpenAIServer(require_key="k1") as srv:
        OpenAICompatProvider(_config(srv.base_url, key="k1")).check_credentials()
        with pytest.raises(CredentialsRejected):
            OpenAICompatProvider(_config(srv.base_url, key="bad")).check_credentials()


def test_check_credentials_tolerates_missing_models_endpoint():
    # LiteLLM/vLLM proxies often don't serve /models — configured but
    # unverifiable is a pass, not a failure.
    with FakeOpenAIServer(serve_models=False) as srv:
        OpenAICompatProvider(_config(srv.base_url)).check_credentials()


# -- health report ------------------------------------------------------------


def test_health_report_openai_branch():
    from vetromar.operations import health_report

    report = health_report(Config(backend="api", ai_provider="openai",
                                  openai_base_url="http://127.0.0.1:1/v1",
                                  api_key=None, cloud_token=None))
    check = next(c for c in report["checks"] if "OpenAI-compatible" in c["label"])
    assert check["ok"] is True and "127.0.0.1:1" in check["detail"]

    report = health_report(Config(backend="api", ai_provider="openai",
                                  openai_base_url=None, api_key=None, cloud_token=None))
    check = next(c for c in report["checks"] if "OpenAI-compatible" in c["label"])
    assert check["ok"] is False
    assert report["ready"] is False


# -- anthropic provider: the adaptive-thinking gate ---------------------------


class _FakeParseClient:
    def __init__(self):
        self.kwargs = None
        self.messages = self

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            parsed_output=FindingList(findings=[]), stop_reason="end_turn"
        )


def test_anthropic_thinking_gated_by_model():
    fake = _FakeParseClient()
    provider = AnthropicProvider(
        Config(api_model="claude-haiku-4-5", api_key="sk-ant-x"), client=fake
    )
    provider.parse_structured(system="s", user="u", schema=FindingList)
    assert "thinking" not in fake.kwargs

    fake2 = _FakeParseClient()
    provider2 = AnthropicProvider(
        Config(api_model="claude-opus-5", api_key="sk-ant-x"), client=fake2
    )
    provider2.parse_structured(system="s", user="u", schema=FindingList)
    assert fake2.kwargs["thinking"] == {"type": "adaptive"}
