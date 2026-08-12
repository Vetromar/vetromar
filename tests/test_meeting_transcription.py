"""Meeting-capture transcription: channel composition, splitting, merging,
and the Deepgram multichannel request — all without real audio models."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

import vetromar.transcription.deepgram as dg
import vetromar.transcription.meeting as meeting_mod
from vetromar.capture.meeting import _has_audio, compose_stereo
from vetromar.schema import Transcript, TranscriptSegment
from vetromar.transcription.deepgram import DeepgramBackend
from vetromar.transcription.meeting import (
    merge_channel_transcripts,
    split_stereo,
    transcribe_meeting,
)


def _write_wav(path, samples, channels=1, samplerate=16000):
    data = np.asarray(samples, dtype="int16")
    if channels == 1 and data.ndim == 1:
        data = data.reshape(-1, 1)
    sf.write(str(path), data, samplerate, subtype="PCM_16")
    return path


def _seg(speaker, text, start_ms, end_ms):
    return TranscriptSegment(speaker=speaker, text=text, start_ms=start_ms, end_ms=end_ms)


# -- merge --------------------------------------------------------------------


def test_merge_interleaves_chronologically():
    mic = [_seg("SPEAKER_00", "hello", 0, 900), _seg("SPEAKER_00", "sure", 5000, 5600)]
    system = [_seg("SPEAKER_01", "hi there", 1000, 2400), _seg("SPEAKER_01", "bye", 8000, 8500)]
    transcript = merge_channel_transcripts(mic, system)
    assert [s.text for s in transcript.segments] == ["hello", "hi there", "sure", "bye"]
    assert [s.speaker for s in transcript.segments] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
        "SPEAKER_01",
    ]


def test_merge_tie_breaks_on_end_ms():
    a = [_seg("SPEAKER_00", "short", 100, 200)]
    b = [_seg("SPEAKER_01", "long", 100, 900)]
    transcript = merge_channel_transcripts(b, a)
    assert [s.text for s in transcript.segments] == ["short", "long"]


# -- stereo compose / split ---------------------------------------------------


def test_compose_then_split_round_trips(tmp_path):
    mic = _write_wav(tmp_path / "mic.wav", [100, 200, 300, 400])
    system = _write_wav(tmp_path / "sys.wav", [-100, -200, -300])
    stereo = tmp_path / "meeting.wav"
    compose_stereo(mic, system, stereo, system_offset_s=0.0)

    left, right = split_stereo(stereo, tmp_path)
    left_data, _ = sf.read(str(left), dtype="int16")
    right_data, _ = sf.read(str(right), dtype="int16")
    assert list(left_data) == [100, 200, 300, 400]
    # The shorter channel is padded with trailing silence to equal length.
    assert list(right_data) == [-100, -200, -300, 0]


def test_compose_pads_later_starting_system_channel(tmp_path):
    mic = _write_wav(tmp_path / "mic.wav", [1] * 16000)  # 1s of signal
    system = _write_wav(tmp_path / "sys.wav", [7] * 8000)
    stereo = tmp_path / "meeting.wav"
    # System tap came up 0.5s after the mic.
    compose_stereo(mic, system, stereo, system_offset_s=0.5)

    data, rate = sf.read(str(stereo), dtype="int16")
    assert rate == 16000
    assert data.shape[1] == 2
    # First half-second of the system channel is silence, then the signal.
    assert not data[:8000, 1].any()
    assert data[8000, 1] == 7
    assert data[0, 0] == 1


def test_compose_pads_later_starting_mic_channel(tmp_path):
    mic = _write_wav(tmp_path / "mic.wav", [3] * 4000)
    system = _write_wav(tmp_path / "sys.wav", [9] * 8000)
    stereo = tmp_path / "meeting.wav"
    compose_stereo(mic, system, stereo, system_offset_s=-0.25)  # mic started later

    data, _ = sf.read(str(stereo), dtype="int16")
    assert not data[:4000, 0].any()
    assert data[4000, 0] == 3
    assert data[0, 1] == 9


def test_has_audio_rejects_silence(tmp_path):
    silent = _write_wav(tmp_path / "silent.wav", [0] * 1000)
    voiced = _write_wav(tmp_path / "voiced.wav", [0] * 500 + [42] + [0] * 100)
    assert _has_audio(silent) is False
    assert _has_audio(voiced) is True
    assert _has_audio(tmp_path / "missing.wav") is False


# -- Deepgram multichannel ----------------------------------------------------

MULTICHANNEL_RESPONSE = {
    "results": {
        "utterances": [
            {"channel": 1, "transcript": "Welcome everyone.", "start": 0.5, "end": 2.0},
            {"channel": 0, "transcript": "Thanks, glad to be here.", "start": 2.2, "end": 4.0},
            {"channel": 1, "transcript": "  ", "start": 4.1, "end": 4.2},
        ]
    }
}


def test_multichannel_maps_channels_to_speakers(tmp_path, monkeypatch):
    wav = tmp_path / "meeting.wav"
    wav.write_bytes(b"RIFF-fake-audio")
    monkeypatch.setattr(dg, "_request", lambda *a, **k: MULTICHANNEL_RESPONSE)
    transcript = DeepgramBackend(api_key="dg-key").transcribe_multichannel(wav)

    assert [s.speaker for s in transcript.segments] == ["SPEAKER_01", "SPEAKER_00"]
    assert transcript.segments[0].text == "Welcome everyone."
    assert (transcript.segments[0].start_ms, transcript.segments[0].end_ms) == (500, 2000)


def test_multichannel_request_assembly(tmp_path, monkeypatch):
    wav = tmp_path / "meeting.wav"
    wav.write_bytes(b"RIFF-fake-audio")
    seen = {}

    def capture(url, body, headers, timeout):
        seen["url"] = url
        return {"results": {}}

    monkeypatch.setattr(dg, "_request", capture)
    DeepgramBackend(api_key="dg-key", model="nova-3").transcribe_multichannel(wav)

    for param in ("model=nova-3", "multichannel=true", "utterances=true", "smart_format=true"):
        assert param in seen["url"]
    assert "diarize" not in seen["url"]


# -- transcribe_meeting dispatch ---------------------------------------------


class _FakeBackend:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path, progress=None):
        self.calls.append(("transcribe", audio_path))
        return Transcript(segments=[])


def test_mono_recording_falls_back_to_normal_backend(tmp_path, monkeypatch):
    mono = _write_wav(tmp_path / "mic-only.wav", [5] * 100)
    fake = _FakeBackend()
    monkeypatch.setattr(meeting_mod, "make_transcription_backend", lambda config: fake)
    transcribe_meeting(mono, config=None)
    assert fake.calls and fake.calls[0][0] == "transcribe"


def test_stereo_recording_uses_multichannel_on_deepgram(tmp_path, monkeypatch):
    stereo = tmp_path / "meeting.wav"
    _write_wav(stereo, np.stack([[1] * 100, [2] * 100], axis=1), channels=2)
    monkeypatch.setattr(dg, "_request", lambda *a, **k: MULTICHANNEL_RESPONSE)
    backend = DeepgramBackend(api_key="dg-key")
    monkeypatch.setattr(meeting_mod, "make_transcription_backend", lambda config: backend)
    transcript = transcribe_meeting(stereo, config=None)
    assert [s.speaker for s in transcript.segments] == ["SPEAKER_01", "SPEAKER_00"]


# -- silence guard ------------------------------------------------------------


def test_empty_transcript_fails_with_friendly_error(tmp_path, monkeypatch):
    from vetromar.capture.meeting import run_meeting_pipeline
    from vetromar.capture.pipeline import _require_speech
    from vetromar.errors import ConfigError

    with pytest.raises(ConfigError, match="No speech"):
        _require_speech(Transcript(segments=[]))

    # And the meeting pipeline surfaces it before touching extraction/store.
    mono = _write_wav(tmp_path / "silent.wav", [0] * 100)
    fake = _FakeBackend()  # returns an empty transcript
    monkeypatch.setattr(meeting_mod, "make_transcription_backend", lambda config: fake)

    class _Cfg:
        db_path = tmp_path / "store.db"

    with pytest.raises(ConfigError, match="No speech"):
        run_meeting_pipeline(mono, "t", _Cfg(), store=None)
