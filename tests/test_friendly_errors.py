"""The backend seams turn setup failures into a friendly ConfigError."""

import ollama
import pytest

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction.api_backend import ApiBackend
from vetromar.extraction.base import make_backend
from vetromar.extraction.local_backend import LocalBackend


def test_api_backend_no_access_raises_configerror():
    with pytest.raises(ConfigError) as exc:
        ApiBackend(Config(api_key=None, cloud_token=None))
    assert "No Anthropic API key" in str(exc.value)


def test_api_backend_with_dev_key_constructs():
    # Constructing the client is offline; no network until extract().
    ApiBackend(Config(api_key="sk-ant-anything"))


def test_workspace_token_grants_no_ai_backend():
    # Post-pivot: a workspace session is sync-only.
    with pytest.raises(ConfigError):
        ApiBackend(Config(api_key=None, cloud_token="tok_ws"))


def test_make_backend_api_without_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        make_backend(Config(backend="api", api_key=None))


def test_local_backend_connection_refused_is_friendly(billing_transcript, monkeypatch):
    def refused(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(ollama, "chat", refused)
    with pytest.raises(ConfigError) as exc:
        LocalBackend(model="qwen3.5:9b").extract(billing_transcript)
    assert "vetromar setup" in str(exc.value)


def test_local_backend_missing_model_is_friendly(billing_transcript, monkeypatch):
    def missing(*a, **k):
        raise ollama.ResponseError("model 'qwen3.5:9b' not found, try pulling it first")

    monkeypatch.setattr(ollama, "chat", missing)
    with pytest.raises(ConfigError) as exc:
        LocalBackend(model="qwen3.5:9b").extract(billing_transcript)
    assert "isn't installed" in str(exc.value)


def test_deepgram_backend_missing_key_raises_configerror():
    from vetromar.transcription.deepgram import DeepgramBackend

    with pytest.raises(ConfigError) as exc:
        DeepgramBackend(api_key=None)
    assert "DEEPGRAM_API_KEY" in str(exc.value)


def test_transcription_factory_cloud_without_key_raises(monkeypatch):
    from vetromar.transcription.base import make_transcription_backend

    with pytest.raises(ConfigError):
        make_transcription_backend(Config(transcribe="cloud", deepgram_api_key=None))
