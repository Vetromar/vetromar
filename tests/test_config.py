"""Config resolution: env > config.toml > default, plus the credentials seam."""

import stat

import pytest

from vetromar import config as cfg
from vetromar.config import load_config, save_api_key, save_config, save_deepgram_api_key


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point config.toml + credentials at a temp dir and clear overriding env."""
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(cfg, "CREDENTIALS_PATH", tmp_path / "credentials")
    monkeypatch.setattr(cfg, "DEEPGRAM_CREDENTIALS_PATH", tmp_path / "credentials-deepgram")
    for key in (
        "VETROMAR_BACKEND", "VETROMAR_LOCAL_MODEL", "VETROMAR_LOCAL_SEED",
        "VETROMAR_DB", "OLLAMA_HOST", "VETROMAR_OLLAMA_HOST", "ANTHROPIC_API_KEY",
        "VETROMAR_TRANSCRIBE", "DEEPGRAM_API_KEY", "VETROMAR_DEEPGRAM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_defaults_when_no_env_no_file(isolated):
    c = load_config()
    assert c.backend == "api"
    assert c.local_model == "qwen3.5:9b"
    assert c.local_seed == 42


def test_file_overrides_default(isolated):
    save_config({"backend": "local", "local_model": "qwen3.5:27b"})
    c = load_config()
    assert c.backend == "local"
    assert c.local_model == "qwen3.5:27b"


def test_env_overrides_file(isolated, monkeypatch):
    save_config({"backend": "local"})
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    assert load_config().backend == "api"


def test_save_config_merges_not_clobbers(isolated):
    save_config({"backend": "local"})
    save_config({"local_model": "qwen3.5:27b"})
    c = load_config()
    assert c.backend == "local"
    assert c.local_model == "qwen3.5:27b"


def test_int_field_cast_from_file(isolated):
    save_config({"local_seed": 43})
    assert load_config().local_seed == 43


def test_ollama_host_defaults_to_dedicated_port(isolated):
    # Not Ollama's default 11434 — a Vetromar-dedicated port so we never collide
    # with or reuse a system Ollama.
    assert load_config().ollama_host == cfg.DEFAULT_OLLAMA_HOST
    assert "11434" not in cfg.DEFAULT_OLLAMA_HOST


def test_generic_ollama_host_env_is_ignored(isolated, monkeypatch):
    # An Ollama user may have OLLAMA_HOST set to their own instance; it must NOT
    # steer Vetromar. Only VETROMAR_OLLAMA_HOST does.
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    assert load_config().ollama_host == cfg.DEFAULT_OLLAMA_HOST
    monkeypatch.setenv("VETROMAR_OLLAMA_HOST", "http://127.0.0.1:9999")
    assert load_config().ollama_host == "http://127.0.0.1:9999"


def test_api_key_from_credentials(isolated):
    save_api_key("sk-ant-test")
    assert load_config().api_key == "sk-ant-test"


def test_env_key_beats_credentials(isolated, monkeypatch):
    save_api_key("sk-from-file")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert load_config().api_key == "sk-from-env"


def test_api_key_never_written_to_config_toml(isolated):
    save_api_key("sk-secret")
    save_config({"backend": "api"})
    assert "sk-secret" not in (isolated / "config.toml").read_text()


def test_credentials_file_is_0600(isolated):
    path = save_api_key("sk-secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- transcription tier (M12) --------------------------------------------------


def test_transcribe_defaults_to_auto(isolated):
    c = load_config()
    assert c.transcribe == "auto"
    assert c.deepgram_api_key is None
    assert c.deepgram_model == "nova-3"


def test_transcribe_file_then_env(isolated, monkeypatch):
    save_config({"transcribe": "local"})
    assert load_config().transcribe == "local"
    monkeypatch.setenv("VETROMAR_TRANSCRIBE", "cloud")
    assert load_config().transcribe == "cloud"


def test_deepgram_key_from_credentials(isolated):
    save_deepgram_api_key("dg-test")
    assert load_config().deepgram_api_key == "dg-test"


def test_deepgram_env_key_beats_credentials(isolated, monkeypatch):
    save_deepgram_api_key("dg-from-file")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-from-env")
    assert load_config().deepgram_api_key == "dg-from-env"


def test_deepgram_key_never_written_to_config_toml(isolated):
    save_deepgram_api_key("dg-secret")
    save_config({"transcribe": "cloud"})
    assert "dg-secret" not in (isolated / "config.toml").read_text()


def test_deepgram_credentials_file_is_0600(isolated):
    path = save_deepgram_api_key("dg-secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- auto-sync (M13 background scheduler) -------------------------------------


def test_auto_sync_defaults_off(isolated, monkeypatch):
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_INTERVAL_MINUTES", raising=False)
    c = load_config()
    assert c.auto_sync_enabled is False
    assert c.auto_sync_interval_minutes == 60


def test_auto_sync_env_false_string_parses_false(isolated, monkeypatch):
    # bool("false") is True — the _as_bool cast is load-bearing for env vars.
    save_config({"auto_sync_enabled": True})
    monkeypatch.setenv("VETROMAR_AUTO_SYNC_ENABLED", "false")
    assert load_config().auto_sync_enabled is False
    monkeypatch.setenv("VETROMAR_AUTO_SYNC_ENABLED", "true")
    assert load_config().auto_sync_enabled is True


def test_auto_sync_toml_roundtrip(isolated, monkeypatch):
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("VETROMAR_AUTO_SYNC_INTERVAL_MINUTES", raising=False)
    save_config({"auto_sync_enabled": True, "auto_sync_interval_minutes": 15})
    c = load_config()
    assert c.auto_sync_enabled is True
    assert c.auto_sync_interval_minutes == 15
