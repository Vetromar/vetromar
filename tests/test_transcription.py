"""The transcription seam: mode resolution, the Deepgram backend, pipeline wiring."""

import urllib.error

import pytest

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.schema import Transcript
from vetromar.transcription import deepgram as dg
from vetromar.transcription.base import make_transcription_backend, resolve_transcription_mode
from vetromar.transcription.deepgram import DeepgramBackend
from vetromar.transcription.local import LocalWhisperXBackend


def _config(**overrides):
    defaults = dict(transcribe="auto", deepgram_api_key=None)
    return Config(**{**defaults, **overrides})


# -- mode resolution -----------------------------------------------------------


def test_auto_without_key_is_local():
    assert resolve_transcription_mode(_config()) == "local"


def test_auto_with_key_is_cloud():
    assert resolve_transcription_mode(_config(deepgram_api_key="dg-key")) == "cloud"


def test_explicit_local_beats_present_key():
    assert resolve_transcription_mode(_config(transcribe="local", deepgram_api_key="dg-key")) == "local"


def test_explicit_cloud_without_access_raises_configerror():
    with pytest.raises(ConfigError) as exc:
        make_transcription_backend(_config(transcribe="cloud", cloud_token=None))
    assert "Deepgram API key" in str(exc.value)


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        resolve_transcription_mode(_config(transcribe="turbo"))


def test_factory_picks_backends():
    assert isinstance(make_transcription_backend(_config()), LocalWhisperXBackend)
    assert isinstance(
        make_transcription_backend(_config(deepgram_api_key="dg-key")), DeepgramBackend
    )


# -- Deepgram backend ----------------------------------------------------------

DEEPGRAM_RESPONSE = {
    "results": {
        "utterances": [
            {"speaker": 0, "transcript": "Decision: billing comes off the monolith.",
             "start": 1.25, "end": 4.5},
            {"speaker": 1, "transcript": "  ", "start": 4.6, "end": 4.9},
            {"speaker": 1, "transcript": "Agreed, I'll write it up.", "start": 5.0, "end": 7.75},
            {"transcript": "Unattributed aside.", "start": 8.0, "end": 9.0},
        ]
    }
}


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "meeting.wav"
    path.write_bytes(b"RIFF-fake-audio")
    return path


def test_deepgram_maps_utterances_to_transcript(wav, monkeypatch):
    monkeypatch.setattr(dg, "_request", lambda *a, **k: DEEPGRAM_RESPONSE)
    transcript = DeepgramBackend(api_key="dg-key").transcribe(wav)

    assert [s.speaker for s in transcript.segments] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_UNKNOWN",  # empty-text utterance dropped
    ]
    first = transcript.segments[0]
    assert first.text == "Decision: billing comes off the monolith."
    assert (first.start_ms, first.end_ms) == (1250, 4500)


def test_deepgram_empty_utterances_yield_empty_transcript(wav, monkeypatch):
    monkeypatch.setattr(dg, "_request", lambda *a, **k: {"results": {}})
    transcript = DeepgramBackend(api_key="dg-key").transcribe(wav)
    assert transcript == Transcript(segments=[])


def test_deepgram_request_assembly(wav, monkeypatch):
    seen = {}

    def capture(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=headers)
        return {"results": {}}

    monkeypatch.setattr(dg, "_request", capture)
    DeepgramBackend(api_key="dg-key", model="nova-3").transcribe(wav)

    assert seen["url"].startswith("https://api.deepgram.com/v1/listen?")
    for param in ("model=nova-3", "diarize=true", "utterances=true", "smart_format=true"):
        assert param in seen["url"]
    assert seen["headers"]["Authorization"] == "Token dg-key"
    assert seen["headers"]["Content-Type"] == "audio/x-wav" or seen["headers"][
        "Content-Type"
    ].startswith("audio/")
    assert seen["body"] == b"RIFF-fake-audio"


def test_deepgram_missing_key_raises_configerror():
    with pytest.raises(ConfigError) as exc:
        DeepgramBackend(api_key=None)
    assert "DEEPGRAM_API_KEY" in str(exc.value)


def _http_error(code):
    import io

    return urllib.error.HTTPError(
        url="https://api.deepgram.com/v1/listen", code=code, msg="err", hdrs=None,
        fp=io.BytesIO(b'{"err_msg": "nope"}'),
    )


def test_deepgram_401_is_friendly(wav, monkeypatch):
    def rejected(*a, **k):
        raise _http_error(401)

    monkeypatch.setattr(dg, "_request", rejected)
    with pytest.raises(ConfigError) as exc:
        DeepgramBackend(api_key="dg-bad").transcribe(wav)
    assert "rejected" in str(exc.value)


def test_deepgram_unreachable_is_friendly(wav, monkeypatch):
    def unreachable(*a, **k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(dg, "_request", unreachable)
    with pytest.raises(ConfigError) as exc:
        DeepgramBackend(api_key="dg-key").transcribe(wav)
    assert "VETROMAR_TRANSCRIBE=local" in str(exc.value)


def test_deepgram_other_http_error_carries_detail(wav, monkeypatch):
    def server_error(*a, **k):
        raise _http_error(503)

    monkeypatch.setattr(dg, "_request", server_error)
    with pytest.raises(RuntimeError) as exc:
        DeepgramBackend(api_key="dg-key").transcribe(wav)
    assert "503" in str(exc.value)


def test_deepgram_reports_progress_stages(wav, monkeypatch):
    monkeypatch.setattr(dg, "_request", lambda *a, **k: {"results": {}})
    stages = []
    DeepgramBackend(api_key="dg-key").transcribe(wav, progress=lambda s, p: stages.append(s))
    assert stages == ["Uploading audio", "Transcribing in cloud (Deepgram)"]


# -- operations: health + key validation ---------------------------------------


def test_health_auto_without_key_stays_ready(tmp_path):
    from tests.conftest import warm_fake_transcription_caches
    from vetromar import operations

    # Auto without a Deepgram key falls to local — which is only ready once
    # the transcription weights are downloaded (M17: downloads are explicit).
    warm_fake_transcription_caches()
    report = operations.health_report(
        _config(backend="api", api_key="sk-ant-x", db_path=tmp_path / "s.db", runtime_dir=tmp_path / "rt")
    )
    assert report["transcription"] == "local"
    assert report["ready"] is True


def test_health_auto_local_without_weights_not_ready(tmp_path):
    from vetromar import operations

    report = operations.health_report(
        _config(backend="api", api_key="sk-ant-x", db_path=tmp_path / "s.db", runtime_dir=tmp_path / "rt")
    )
    assert report["transcription"] == "local"
    assert report["ready"] is False
    check = next(c for c in report["checks"] if c["label"] == "local transcription models")
    assert check["ok"] is False
    assert "Download local models" in check["detail"]


def test_health_explicit_cloud_without_key_not_ready(tmp_path):
    from vetromar import operations

    report = operations.health_report(
        _config(backend="api", api_key="sk-ant-x", transcribe="cloud", db_path=tmp_path / "s.db", runtime_dir=tmp_path / "rt")
    )
    assert report["transcription"] == "cloud"
    assert report["ready"] is False
    assert any(c["label"] == "cloud transcription access" and not c["ok"] for c in report["checks"])


def test_health_auto_with_key_is_cloud(tmp_path):
    from vetromar import operations

    report = operations.health_report(
        _config(backend="api", api_key="sk-ant-x", deepgram_api_key="dg-k", db_path=tmp_path / "s.db", runtime_dir=tmp_path / "rt")
    )
    assert report["transcription"] == "cloud"
    assert report["ready"] is True


def test_validate_and_save_deepgram_key(monkeypatch, tmp_path):
    from vetromar import config as cfg
    from vetromar import operations

    monkeypatch.setattr(cfg, "DEEPGRAM_CREDENTIALS_PATH", tmp_path / "credentials-deepgram")
    monkeypatch.setattr(operations, "_check_deepgram_key", lambda key: key == "dg-valid")

    with pytest.raises(operations.InvalidApiKey):
        operations.validate_and_save_deepgram_key("dg-bogus")
    assert not (tmp_path / "credentials-deepgram").exists()

    operations.validate_and_save_deepgram_key(" dg-valid \n")
    assert (tmp_path / "credentials-deepgram").read_text().strip() == "dg-valid"


# -- pipeline wiring -----------------------------------------------------------


def test_run_pipeline_uses_transcription_backend(store, billing_transcript, wav, monkeypatch, tmp_path):
    """run_pipeline routes audio through the seam, persists the transcript, and
    hands it to extraction — proves the swap away from the direct call."""
    from vetromar.capture import pipeline as pl
    from tests.conftest import make_billing_unit

    class StubTranscriber:
        def transcribe(self, audio_path, progress=None):
            return billing_transcript

    class StubExtractor:
        def extract(self, transcript):
            assert transcript is billing_transcript
            return [make_billing_unit()]

    monkeypatch.setattr(pl, "make_transcription_backend", lambda config: StubTranscriber())
    monkeypatch.setattr(pl, "make_backend", lambda config: StubExtractor())

    config = _config(db_path=tmp_path / "store.db")
    episode, units, _ = pl.run_pipeline(wav, title="Seam test", config=config, store=store)

    persisted = tmp_path / "transcripts" / "meeting.json"
    assert persisted.exists()
    assert store.get_unit(units[0].id).provenance.episode_id == episode.id
