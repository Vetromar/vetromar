"""Phase-C sync loop, fully deterministic: the real stdio transport + fake
chat MCP server from test_sources_connect, driven by a scripted fake
Anthropic client that behaves like a competent sync agent (channels ->
messages-since-cursor -> deliver + set_cursor)."""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction.validate import ExtractionGateError
from vetromar.providers.anthropic import AnthropicProvider
from vetromar.sources import sync as sync_mod
from vetromar.sources.registry import SourceConfig
from vetromar.sources.sync import sync_source


def _fake_provider(config, fake) -> AnthropicProvider:
    """The real AnthropicProvider over a scripted wire-shaped fake client, so
    the provider's transcript mechanics stay under test."""
    return AnthropicProvider(config, client=fake)

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _source(extra_json: str | None = None, tools=None) -> SourceConfig:
    env = {"FAKE_CHAT_EXTRA_JSON": extra_json} if extra_json else {}
    return SourceConfig(
        name="fakechat", source_kind="chat",
        command=sys.executable, args=[str(FAKE_SERVER)], env=env, tools=tools,
    )


def _config() -> Config:
    return Config(backend="api", api_key="test-key")


def _block(name, input, id):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def _parse_result(content: str) -> list:
    """Tool results arrive as the JSON of structuredContent: {'result': [...]}."""
    if content == "(empty result)":
        return []
    data = json.loads(content)
    if isinstance(data, dict):
        return data.get("result", [])
    return data


class FakeAnthropic:
    """Scripted agent: list_channels -> list_messages per channel (honoring
    the cursor) -> deliver one episode per non-empty channel + set_cursor."""

    def __init__(self):
        self.stage = 0
        self.cursor: dict = {}
        self.channels: list[str] = []
        self.offered_tool_names: set[str] = set()
        self.messages = self  # sync.py calls client.messages.create(...)

    def _last_results(self, messages) -> dict:
        by_id = {}
        for item in messages[-1]["content"]:
            by_id[item["tool_use_id"]] = item["content"]
        return by_id

    def create(self, *, model, max_tokens, system, tools, messages):
        self.offered_tool_names = {t["name"] for t in tools}
        if self.stage == 0:
            m = re.search(r"Cursor from the last sync: (\{.*\})", messages[0]["content"])
            self.cursor = json.loads(m.group(1)) if m else {}
            self.stage = 1
            return SimpleNamespace(
                content=[_block("list_channels", {}, "tu_ch")], stop_reason="tool_use"
            )
        if self.stage == 1:
            self.channels = list(_parse_result(self._last_results(messages)["tu_ch"]))
            self.stage = 2
            return SimpleNamespace(
                content=[
                    _block(
                        "list_messages",
                        {"channel": ch, "after_ts": self.cursor.get(ch, "")},
                        f"tu_{ch}",
                    )
                    for ch in self.channels
                ],
                stop_reason="tool_use",
            )
        if self.stage == 2:
            results = self._last_results(messages)
            episodes = []
            new_cursor = dict(self.cursor)
            for ch in self.channels:
                msgs = _parse_result(results[f"tu_{ch}"])
                if not msgs:
                    continue
                episodes.append(
                    {
                        "title": f"#{ch} messages",
                        "source_kind": "chat",
                        "external_id": f"fakechat:{ch}:{msgs[0]['ts']}-{msgs[-1]['ts']}",
                        "raw": "\n".join(f"{m['author']}: {m['text']}" for m in msgs),
                    }
                )
                new_cursor[ch] = msgs[-1]["ts"]
            self.stage = 3
            return SimpleNamespace(
                content=[
                    _block("deliver_episodes", {"episodes": episodes}, "tu_del"),
                    _block("set_cursor", {"cursor": json.dumps(new_cursor, sort_keys=True)}, "tu_cur"),
                ],
                stop_reason="tool_use",
            )
        return SimpleNamespace(content=[], stop_reason="end_turn")


@pytest.fixture
def fake_llm(monkeypatch):
    fakes: list[FakeAnthropic] = []

    def factory(config):
        fake = FakeAnthropic()
        fakes.append(fake)
        return _fake_provider(config, fake)

    monkeypatch.setattr(sync_mod, "_make_provider", factory)
    return fakes


def test_first_sync_ingests_and_sets_cursor(store, fake_llm):
    report = sync_source(store, _source(), _config(), extract=False)
    assert sorted(report.created) == [
        "fakechat:design:200.1-200.1",
        "fakechat:eng:100.1-100.3",
    ]
    assert report.duplicates == []
    ep = store.get_episode_by_external_id("fakechat:eng:100.1-100.3")
    assert "priya: agreed, decision: export jobs go on redis streams" in ep.raw
    assert ep.source_kind == "chat"
    cursor, _ = store.get_sync_state("fakechat")
    assert json.loads(cursor) == {"eng": "100.3", "design": "200.1"}
    # the mutating tool was never offered to the model
    assert "post_message" not in fake_llm[0].offered_tool_names
    assert {"list_channels", "list_messages"} <= fake_llm[0].offered_tool_names


def test_resync_after_cursor_is_a_noop(store, fake_llm):
    sync_source(store, _source(), _config(), extract=False)
    report = sync_source(store, _source(), _config(), extract=False)
    assert report.created == []
    assert report.duplicates == []
    assert len(store.list_episodes()) == 2


def test_full_resync_overlap_dedupes(store, fake_llm):
    sync_source(store, _source(), _config(), extract=False)
    report = sync_source(store, _source(), _config(), full=True, extract=False)
    assert report.created == []
    assert sorted(report.duplicates) == [
        "fakechat:design:200.1-200.1",
        "fakechat:eng:100.1-100.3",
    ]
    assert len(store.list_episodes()) == 2


def test_new_message_syncs_as_delta(store, fake_llm):
    sync_source(store, _source(), _config(), extract=False)
    extra = json.dumps(
        [{"ts": "300.1", "channel": "eng", "author": "sam", "text": "rollout done"}]
    )
    report = sync_source(store, _source(extra_json=extra), _config(), extract=False)
    assert report.created == ["fakechat:eng:300.1-300.1"]
    assert len(store.list_episodes()) == 3
    assert json.loads(store.get_sync_state("fakechat")[0])["eng"] == "300.1"


def test_dry_run_writes_nothing(store, fake_llm):
    report = sync_source(store, _source(), _config(), dry_run=True)
    assert len(report.created) == 2
    assert store.list_episodes() == []
    assert store.get_sync_state("fakechat") is None


def test_agent_failure_does_not_advance_cursor(store, fake_llm, monkeypatch):
    class ExplodingFake(FakeAnthropic):
        def create(self, **kwargs):
            if self.stage == 2:
                raise RuntimeError("model fell over")
            return super().create(**kwargs)

    monkeypatch.setattr(
        sync_mod, "_make_provider", lambda config: _fake_provider(config, ExplodingFake())
    )
    with pytest.raises(RuntimeError):
        sync_source(store, _source(), _config(), extract=False)
    assert store.get_sync_state("fakechat") is None
    assert store.list_episodes() == []


def test_extraction_gate_failure_is_not_fatal(store, fake_llm, monkeypatch):
    import vetromar.extraction.generic as generic

    def explode(store_, episode, config):
        raise ExtractionGateError("synthetic gate failure")

    monkeypatch.setattr(generic, "extract_from_raw", explode)
    report = sync_source(store, _source(), _config(), extract=True)
    assert len(report.created) == 2
    assert sorted(report.extraction_failures) == sorted(report.created)
    # raw episodes landed and the cursor still advanced
    assert len(store.list_episodes()) == 2
    assert store.get_sync_state("fakechat") is not None


def test_local_backend_is_config_error(store):
    with pytest.raises(ConfigError):
        sync_source(store, _source(), Config(backend="local", api_key=None))


def test_no_ai_access_is_provider_error(store):
    # No provider configured — the guard names the fix. A workspace token
    # alone grants no AI post-pivot.
    with pytest.raises(ConfigError, match="AI provider"):
        sync_source(
            store, _source(), Config(backend="api", api_key=None, cloud_token="tok_x")
        )


def test_tool_rails():
    read_only = SimpleNamespace(name="whatever", annotations=SimpleNamespace(readOnlyHint=True))
    declared_mutating = SimpleNamespace(name="fetch_stuff", annotations=SimpleNamespace(readOnlyHint=False))
    undeclared_reader = SimpleNamespace(name="list_messages", annotations=None)
    undeclared_writer = SimpleNamespace(name="post_message", annotations=None)
    assert sync_mod._tool_allowed(read_only, None)
    assert not sync_mod._tool_allowed(declared_mutating, None)
    assert sync_mod._tool_allowed(undeclared_reader, None)
    assert not sync_mod._tool_allowed(undeclared_writer, None)
    # an explicit per-source allowlist overrides everything
    assert sync_mod._tool_allowed(undeclared_writer, ["post_message"])
    assert not sync_mod._tool_allowed(undeclared_reader, ["post_message"])


# -- M13 thoroughness: full-mode, nudges, incomplete/continuation -------------

from vetromar.sources.sync_prompt import (  # noqa: E402
    SYNC_NUDGE_PROMPT,
    SYNC_SYSTEM_PROMPT,
    build_sync_user_prompt,
)


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _ns(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def _episode(i):
    return {
        "title": f"page {i}",
        "source_kind": "document",
        "external_id": f"src:p{i}",
        "raw": f"content of page {i}",
    }


class ScriptedFake:
    """Plays back a fixed list of responses (or raises a scripted exception),
    recording every create() call's messages for assertions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.messages = self  # sync.py calls client.messages.create(...)

    def create(self, *, model, max_tokens, system, tools, messages):
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            return _ns([], "end_turn")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _scripted(monkeypatch, script) -> ScriptedFake:
    fake = ScriptedFake(script)
    monkeypatch.setattr(
        sync_mod, "_make_provider", lambda config: _fake_provider(config, fake)
    )
    return fake


def test_full_flag_reaches_the_agent_prompt(store, monkeypatch):
    fake = _scripted(monkeypatch, [
        _ns([_block("set_cursor", {"cursor": "{}"}, "tu_c")], "tool_use"),
    ])
    report = sync_source(store, _source(), _config(), full=True, extract=False)
    assert "FULL SYNC" in fake.calls[0][0]["content"]
    assert report.incomplete is False


def test_incremental_prompt_has_no_full_sync_wording(store, monkeypatch):
    fake = _scripted(monkeypatch, [
        _ns([_block("set_cursor", {"cursor": "{}"}, "tu_c")], "tool_use"),
    ])
    sync_source(store, _source(), _config(), extract=False)
    first_prompt = fake.calls[0][0]["content"]
    assert "FULL SYNC" not in first_prompt
    assert "first sync" in first_prompt


def test_multi_turn_incremental_delivery(store, monkeypatch):
    # The paginating shape rule 5 demands: deliver page 1, deliver page 2,
    # then set_cursor — everything lands, run is complete.
    _scripted(monkeypatch, [
        _ns([_block("deliver_episodes", {"episodes": [_episode(1)]}, "t1")], "tool_use"),
        _ns([_block("deliver_episodes", {"episodes": [_episode(2)]}, "t2")], "tool_use"),
        _ns([_block("set_cursor", {"cursor": "done"}, "t3")], "tool_use"),
    ])
    report = sync_source(store, _source(), _config(), extract=False)
    assert sorted(report.created) == ["src:p1", "src:p2"]
    assert report.incomplete is False
    assert store.get_sync_state("fakechat")[0] == "done"


def test_nudge_recovers_an_early_stop(store, monkeypatch):
    # Cheap-model failure mode: delivers one page, then declares done without
    # set_cursor. The nudge sends it back to finish the sweep.
    fake = _scripted(monkeypatch, [
        _ns([_block("deliver_episodes", {"episodes": [_episode(1)]}, "t1")], "tool_use"),
        _ns([_text("That's everything, I think.")], "end_turn"),
        _ns([
            _block("deliver_episodes", {"episodes": [_episode(2)]}, "t2"),
            _block("set_cursor", {"cursor": "done"}, "t3"),
        ], "tool_use"),
    ])
    report = sync_source(store, _source(), _config(), extract=False)
    assert sorted(report.created) == ["src:p1", "src:p2"]
    assert report.incomplete is False
    assert store.get_sync_state("fakechat")[0] == "done"
    # the checklist nudge was actually sent as a user message
    assert any(
        m["role"] == "user" and m["content"] == SYNC_NUDGE_PROMPT
        for m in fake.calls[-1]
    )


def test_nudge_cap_ends_incomplete_without_cursor(store, monkeypatch):
    stop = lambda: _ns([_text("done.")], "end_turn")  # noqa: E731
    _scripted(monkeypatch, [
        _ns([_block("deliver_episodes", {"episodes": [_episode(1)]}, "t1")], "tool_use"),
        stop(), stop(), stop(),
    ])
    report = sync_source(store, _source(), _config(), extract=False)
    # what WAS fetched is stored; the cursor stays put so a re-run continues
    assert report.created == ["src:p1"]
    assert report.incomplete is True
    assert store.get_sync_state("fakechat") is None
    assert len(store.list_episodes()) == 1


def test_max_turns_exhaustion_is_incomplete_not_lost(store, monkeypatch):
    class EndlessFake:
        def __init__(self):
            self.n = 0
            self.messages = self

        def create(self, **kwargs):
            self.n += 1
            return _ns(
                [_block("deliver_episodes", {"episodes": [_episode(self.n)]}, f"t{self.n}")],
                "tool_use",
            )

    monkeypatch.setattr(sync_mod, "MAX_TURNS", 3)
    monkeypatch.setattr(
        sync_mod, "_make_provider", lambda config: _fake_provider(config, EndlessFake())
    )
    report = sync_source(store, _source(), _config(), extract=False)
    assert len(report.created) == 3
    assert report.incomplete is True
    assert store.get_sync_state("fakechat") is None


def test_midcrawl_model_failure_keeps_deliveries(store, monkeypatch):
    _scripted(monkeypatch, [
        _ns([_block("deliver_episodes", {"episodes": [_episode(1)]}, "t1")], "tool_use"),
        RuntimeError("model fell over mid-crawl"),
    ])
    # no exception: the delivered episode lands, the run ends incomplete
    report = sync_source(store, _source(), _config(), extract=False)
    assert report.created == ["src:p1"]
    assert report.incomplete is True
    assert store.get_sync_state("fakechat") is None


def test_prompt_carries_the_thoroughness_clauses():
    assert "has_more" in SYNC_SYSTEM_PROMPT and "next_cursor" in SYNC_SYSTEM_PROMPT
    assert "child" in SYNC_SYSTEM_PROMPT  # recursion into child pages
    assert "FULL SYNC" in SYNC_SYSTEM_PROMPT
    full = build_sync_user_prompt("s", "chat", None, full=True)
    incremental = build_sync_user_prompt("s", "chat", None)
    assert full != incremental
    assert "FULL SYNC" in full and "FULL SYNC" not in incremental
