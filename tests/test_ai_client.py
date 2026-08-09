"""vetromar.ai — BYO provider resolution and error translation."""

from __future__ import annotations

import httpx
import pytest

from vetromar import ai
from vetromar.config import Config
from vetromar.errors import ConfigError


def _config(**overrides):
    defaults = dict(api_key=None, cloud_token=None, deepgram_api_key=None)
    return Config(**{**defaults, **overrides})


# -- anthropic_client resolution ----------------------------------------------


def test_dev_key_uses_default_base_url():
    client = ai.anthropic_client(_config(api_key="sk-ant-dev"))
    assert "anthropic.com" in str(client.base_url)


def test_no_key_raises_configerror():
    with pytest.raises(ConfigError) as exc:
        ai.anthropic_client(_config())
    assert "No Anthropic API key" in str(exc.value)


def test_workspace_token_alone_grants_no_ai():
    # Post-pivot: a workspace session is sync-only; AI needs the user's own
    # provider.
    assert ai.ai_available(_config(cloud_token="tok_ws")) is False
    with pytest.raises(ConfigError):
        ai.anthropic_client(_config(cloud_token="tok_ws"))


def test_ai_available_by_provider():
    assert ai.ai_available(_config(api_key="k")) is True
    assert ai.ai_available(_config()) is False
    assert (
        ai.ai_available(_config(ai_provider="openai", openai_base_url="http://x/v1"))
        is True
    )
    # A dangling anthropic key does not make the SELECTED openai provider work.
    assert ai.ai_available(_config(ai_provider="openai", api_key="k")) is False


# -- deepgram_target ----------------------------------------------------------


def test_deepgram_target_own_key_direct():
    url, headers = ai.deepgram_target(_config(deepgram_api_key="dg-dev"))
    assert url == "https://api.deepgram.com/v1/listen"
    assert headers["Authorization"] == "Token dg-dev"


def test_deepgram_target_none_without_key():
    assert ai.deepgram_target(_config()) is None
    # A workspace session no longer implies cloud transcription.
    assert ai.deepgram_target(_config(cloud_token="tok_ws")) is None


# -- map_ai_error -------------------------------------------------------------


def _status_error(status: int, body: dict | None = None):
    anthropic = pytest.importorskip("anthropic")
    body = body or {"error": {"type": "err", "message": "boom"}}
    request = httpx.Request("POST", "http://x/v1/messages")
    response = httpx.Response(status, json=body, request=request)
    if status == 401:
        return anthropic.AuthenticationError("auth", response=response, body=body)
    return anthropic.APIStatusError("status", response=response, body=body)


def test_rejected_key_is_translated():
    err = ai.map_ai_error(_status_error(401), _config(api_key="sk-ant-dev"))
    assert "rejected the API key" in err.message


def test_other_statuses_not_translated():
    assert ai.map_ai_error(_status_error(500), _config(api_key="sk-ant-dev")) is None


def test_unconfigured_provider_translates_nothing():
    assert ai.map_ai_error(_status_error(401), _config()) is None


def test_non_sdk_error_not_translated():
    assert ai.map_ai_error(RuntimeError("x"), _config(api_key="k")) is None


# -- transcription auto mode --------------------------------------------------


def test_auto_mode_is_cloud_iff_deepgram_key():
    from vetromar.transcription.base import resolve_transcription_mode

    assert resolve_transcription_mode(_config(transcribe="auto", deepgram_api_key="dg")) == "cloud"
    assert resolve_transcription_mode(_config(transcribe="auto")) == "local"
    # A workspace session no longer flips auto to cloud.
    assert resolve_transcription_mode(_config(transcribe="auto", cloud_token="t")) == "local"


def test_factory_cloud_uses_own_key():
    from vetromar.transcription.base import make_transcription_backend

    backend = make_transcription_backend(_config(transcribe="cloud", deepgram_api_key="dg-k"))
    assert backend._listen_url == "https://api.deepgram.com/v1/listen"
    assert backend._auth_headers["Authorization"] == "Token dg-k"


def test_factory_explicit_cloud_without_key_raises():
    from vetromar.transcription.base import make_transcription_backend

    with pytest.raises(ConfigError, match="Deepgram API key"):
        make_transcription_backend(_config(transcribe="cloud", cloud_token="tok_ws"))
