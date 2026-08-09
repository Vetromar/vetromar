"""CLI capture-path wiring: the `record` mic command routes into the pipeline,
and local mode guarantees the Ollama server before extraction."""

import os
import re
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

import vetromar.capture.audio as audio_mod
import vetromar.capture.pipeline as pipeline_mod
import vetromar.runtime as runtime_mod
from vetromar.cli import app
from vetromar.schema import Episode

runner = CliRunner()


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Point every on-disk side effect at a scratch dir and force a backend that
    needs no live service, so these are pure wiring tests."""
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))  # no real config
    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return tmp_path


def _fake_pipeline_result():
    episode = Episode(title="t", source_kind="meeting", occurred_at=datetime.now(timezone.utc))
    return episode, [], "# rendered"


def test_record_routes_recorded_wav_into_pipeline(isolated_env, monkeypatch):
    seen = {}

    def fake_record_mic(out_path, duration_s=None, samplerate=16000):
        seen["out_path"] = out_path
        seen["duration"] = duration_s
        return out_path

    def fake_run_pipeline(audio_path, **kwargs):
        seen["pipeline_audio"] = audio_path
        return _fake_pipeline_result()

    monkeypatch.setattr(audio_mod, "record_mic", fake_record_mic)
    monkeypatch.setattr(pipeline_mod, "run_pipeline", fake_run_pipeline)

    result = runner.invoke(app, ["record", "--title", "Standup", "--duration", "5"])

    assert result.exit_code == 0, result.output
    assert seen["duration"] == 5
    # The exact file the mic wrote is the one handed to the pipeline.
    assert seen["pipeline_audio"] == seen["out_path"]
    assert seen["out_path"].suffix == ".wav"


def test_record_keep_path_is_honored(isolated_env, monkeypatch):
    keep = isolated_env / "meeting.wav"
    captured = {}

    monkeypatch.setattr(audio_mod, "record_mic", lambda out_path, **k: captured.setdefault("p", out_path) or out_path)
    monkeypatch.setattr(pipeline_mod, "run_pipeline", lambda audio_path, **k: _fake_pipeline_result())

    result = runner.invoke(app, ["record", "-t", "M", "--keep", str(keep)])

    assert result.exit_code == 0, result.output
    assert captured["p"] == keep


def test_capture_and_record_without_title_get_dated_default(isolated_env, monkeypatch):
    seen = {}

    def fake_run_pipeline(audio_path, **kwargs):
        seen["title"] = kwargs["title"]
        return _fake_pipeline_result()

    monkeypatch.setattr(audio_mod, "record_mic", lambda out_path, **k: out_path)
    monkeypatch.setattr(pipeline_mod, "run_pipeline", fake_run_pipeline)

    for args in (["capture", "some.wav"], ["record", "--duration", "1"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert re.fullmatch(r"Meeting \d{4}-\d{2}-\d{2} \d{2}:\d{2}", seen["title"])


def test_capture_local_mode_ensures_server_and_pins_host(isolated_env, monkeypatch):
    monkeypatch.setenv("VETROMAR_BACKEND", "local")
    monkeypatch.setenv("VETROMAR_OLLAMA_HOST", "http://127.0.0.1:11435")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    calls = {"ensure_server": 0}

    monkeypatch.setattr(runtime_mod, "ensure_server", lambda config: calls.__setitem__("ensure_server", calls["ensure_server"] + 1))
    monkeypatch.setattr(pipeline_mod, "run_pipeline", lambda audio_path, **k: _fake_pipeline_result())

    result = runner.invoke(app, ["capture", "some.wav", "--title", "M"])

    assert result.exit_code == 0, result.output
    assert calls["ensure_server"] == 1
    # The frozen ollama.chat default client is pinned to OUR managed server.
    assert os.environ["OLLAMA_HOST"] == "http://127.0.0.1:11435"


def test_capture_api_mode_does_not_touch_runtime(isolated_env, monkeypatch):
    def boom(config):
        raise AssertionError("ensure_server must not run in api mode")

    monkeypatch.setattr(runtime_mod, "ensure_server", boom)
    monkeypatch.setattr(pipeline_mod, "run_pipeline", lambda audio_path, **k: _fake_pipeline_result())

    result = runner.invoke(app, ["capture", "some.wav", "--title", "M"])

    assert result.exit_code == 0, result.output
