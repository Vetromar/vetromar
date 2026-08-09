"""M17: explicit local-model downloads — presence probes, the unified download
op, and the guard that keeps local capture from silently fetching gigabytes.

The autouse `_no_ambient_model_caches` conftest fixture points HF_HUB_CACHE /
TORCH_HOME at empty scratch dirs; `warm_fake_transcription_caches` fabricates
the exact files the probes look for.
"""

from __future__ import annotations

import pytest

from tests.conftest import warm_fake_transcription_caches
from vetromar import operations
from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.transcription import assets
from vetromar.transcription.local import LocalWhisperXBackend


def _config(tmp_path, **overrides):
    defaults = dict(
        api_key=None,
        cloud_token=None,
        deepgram_api_key=None,
        runtime_dir=tmp_path / "runtime",
        db_path=tmp_path / "store.db",
    )
    return Config(**{**defaults, **overrides})


# -- presence probes -----------------------------------------------------------


def test_status_absent_on_cold_caches(tmp_path):
    status = assets.transcription_models_status(_config(tmp_path))
    assert status["present"] is False
    assert all(not c["present"] for c in status["components"].values())


def test_status_present_after_warming(tmp_path):
    warm_fake_transcription_caches()
    status = assets.transcription_models_status(_config(tmp_path))
    assert status["present"] is True
    assert all(c["present"] for c in status["components"].values())


def test_partial_download_reports_missing_components(tmp_path, monkeypatch):
    warm_fake_transcription_caches()
    # Knock out the align checkpoint: partial state must not probe present.
    import os
    from pathlib import Path

    ckpt = Path(os.environ["TORCH_HOME"]) / "hub" / "checkpoints"
    (ckpt / "wav2vec2_fairseq_base_ls960_asr_ls960.pth").unlink()
    status = assets.transcription_models_status(_config(tmp_path))
    assert status["present"] is False
    assert status["components"]["whisper"]["present"] is True
    assert status["components"]["align"]["present"] is False
    assert assets.missing_component_names(status) == ["alignment model"]


def test_whisper_repo_id_uses_faster_whisper_table_and_falls_back():
    # large-v3 is in faster-whisper's table (the capture extra is installed
    # in the dev venv); an unknown size uses the Systran convention.
    assert assets.whisper_repo_id("large-v3") == "Systran/faster-whisper-large-v3"
    assert assets.whisper_repo_id("not-a-size") == "Systran/faster-whisper-not-a-size"


def test_model_files_present_probes_ollama_manifest(tmp_path):
    from vetromar import runtime

    config = _config(tmp_path, local_model="qwen3.5:9b")
    assert runtime.model_files_present(config) is False
    manifest = (
        config.models_dir / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5" / "9b"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    assert runtime.model_files_present(config) is True


def test_local_models_status_shape(tmp_path):
    status = operations.local_models_status(_config(tmp_path))
    assert set(status) == {"extraction", "transcription", "embedding"}
    assert set(status["extraction"]) == {"model", "runtime", "model_present"}
    assert "present" in status["transcription"]
    assert "cached" in status["embedding"]


# -- the unified download ------------------------------------------------------


def test_download_local_models_runs_all_legs_in_order(tmp_path, monkeypatch):
    calls = []
    from vetromar import runtime

    monkeypatch.setattr(
        runtime, "ensure_local_ready", lambda config, progress=None: calls.append("runtime")
    )
    monkeypatch.setattr(
        assets,
        "download_transcription_models",
        lambda config, progress=None: calls.append("transcription"),
    )
    monkeypatch.setattr(
        operations, "prefetch_embedding_model", lambda progress=None: calls.append("embedding")
    )
    operations.download_local_models(_config(tmp_path))
    assert calls == ["runtime", "transcription", "embedding"]


def test_download_transcription_models_skips_present_components(tmp_path, monkeypatch):
    pytest.importorskip("faster_whisper")  # the [capture] extra — absent on CI
    warm_fake_transcription_caches()
    import faster_whisper.utils
    import huggingface_hub
    import whisperx

    def boom(*args, **kwargs):
        raise AssertionError("already-present component must not re-download")

    monkeypatch.setattr(faster_whisper.utils, "download_model", boom)
    monkeypatch.setattr(whisperx, "load_align_model", boom)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
    notes = []
    assets.download_transcription_models(
        _config(tmp_path), progress=lambda label, c, t: notes.append(label)
    )
    assert any("ready" in n for n in notes)


def test_download_transcription_models_fetches_missing(tmp_path, monkeypatch):
    pytest.importorskip("faster_whisper")  # the [capture] extra — absent on CI
    calls = []
    import faster_whisper.utils
    import huggingface_hub
    import whisperx

    monkeypatch.setattr(
        faster_whisper.utils, "download_model", lambda model, **kw: calls.append(("whisper", model))
    )
    monkeypatch.setattr(
        whisperx, "load_align_model", lambda lang, device: calls.append(("align", lang))
    )
    monkeypatch.setattr(
        huggingface_hub, "snapshot_download", lambda repo, **kw: calls.append(("dia", repo))
    )
    config = _config(tmp_path)
    assets.download_transcription_models(config)
    assert calls == [
        ("whisper", config.whisper_model),
        ("align", "en"),
        ("dia", config.diarization_model),
    ]


# -- the local-capture guard ---------------------------------------------------


def test_local_backend_refuses_without_weights(tmp_path, monkeypatch):
    import vetromar.transcription.local as local_mod

    def boom(*args, **kwargs):
        raise AssertionError("transcription must not run without weights")

    monkeypatch.setattr(local_mod, "transcribe_and_diarize", boom)
    backend = LocalWhisperXBackend(_config(tmp_path))
    with pytest.raises(ConfigError) as exc:
        backend.transcribe(tmp_path / "a.wav")
    assert "aren't downloaded" in str(exc.value)
    assert "Download local models" in (exc.value.hint or "")


def test_local_backend_runs_with_weights(tmp_path, monkeypatch):
    import vetromar.transcription.local as local_mod

    warm_fake_transcription_caches()
    seen = {}

    def fake_transcribe(path, config, progress=None):
        seen["path"] = path
        return "transcript"

    monkeypatch.setattr(local_mod, "transcribe_and_diarize", fake_transcribe)
    backend = LocalWhisperXBackend(_config(tmp_path))
    assert backend.transcribe(tmp_path / "a.wav") == "transcript"
    assert seen["path"] == tmp_path / "a.wav"


# -- backend switching ---------------------------------------------------------


def test_select_local_backend_writes_fully_local_no_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    from vetromar import runtime

    def boom(*args, **kwargs):
        raise AssertionError("selecting local mode must not download anything")

    monkeypatch.setattr(runtime, "ensure_local_ready", boom)
    monkeypatch.setattr(assets, "download_transcription_models", boom)
    operations.select_local_backend(_config(tmp_path))
    text = (tmp_path / "config.toml").read_text()
    assert 'backend = "local"' in text
    assert 'transcribe = "local"' in text


def test_select_api_backend_restores_auto_transcription(tmp_path, monkeypatch):
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    operations.select_api_backend(_config(tmp_path, api_key="sk-ant-dev"))
    text = (tmp_path / "config.toml").read_text()
    assert 'backend = "api"' in text
    assert 'transcribe = "auto"' in text


def test_select_api_backend_without_provider_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("VETROMAR_CONFIG", str(tmp_path / "config.toml"))
    with pytest.raises(ConfigError) as exc:
        operations.select_api_backend(_config(tmp_path, api_key=None, cloud_token=None))
    assert "AI provider" in str(exc.value)
    assert not (tmp_path / "config.toml").exists()
