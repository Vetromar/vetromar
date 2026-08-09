"""The pluggable transcription seam — the second privacy-choice boundary.

One interface, two interchangeable backends selected by configuration:
- LocalWhisperXBackend (whisperx + pyannote on this machine; audio never leaves)
- DeepgramBackend      (cloud batch STT with diarization; minutes become seconds)

Both emit the SAME `Transcript` shape — `SPEAKER_NN` labels + ms spans — so
everything downstream (extraction, the frozen quote gate, auto-linking's
SPEAKER-skip rule, the store) is identical regardless of tier. Do not let
backend-specific behavior leak past this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from vetromar.schema import Transcript

# A progress sink: (stage label, percent-of-stage or None for indeterminate).
# Same shape as capture/transcribe.py's ProgressFn — defined here too because
# capture.pipeline imports this module (importing capture back would cycle).
ProgressFn = Callable[[str, Optional[float]], None]


class TranscriptionBackend(ABC):
    @abstractmethod
    def transcribe(
        self, audio_path: str | Path, progress: ProgressFn | None = None
    ) -> Transcript:
        """Turn an audio file into a diarized transcript.

        Implementations must emit `SPEAKER_\\d+` speaker labels and integer
        millisecond spans — the shape the rest of the engine is built on."""


def resolve_transcription_mode(config) -> str:
    """Resolve config.transcribe to the effective tier: 'local' or 'cloud'.

    "auto" (the default) picks cloud iff a Deepgram key is configured, local
    otherwise. The single source of truth: factory, health_report, and doctor
    all call this."""
    mode = config.transcribe
    if mode in ("local", "cloud"):
        return mode
    if mode == "auto":
        return "cloud" if config.deepgram_api_key else "local"
    raise ValueError(
        f"unknown transcription mode: {mode!r} (expected 'auto', 'local' or 'cloud')"
    )


def make_transcription_backend(config) -> TranscriptionBackend:
    """Backend factory driven by config — the customer's privacy choice."""
    mode = resolve_transcription_mode(config)
    if mode == "cloud":
        from vetromar.ai import deepgram_target
        from vetromar.errors import ConfigError
        from vetromar.transcription.deepgram import DeepgramBackend

        target = deepgram_target(config)
        if target is None:
            raise ConfigError(
                "Cloud transcription needs a Deepgram API key.",
                hint="Add one in Settings (or set DEEPGRAM_API_KEY), "
                "or set VETROMAR_TRANSCRIBE=local.",
            )
        listen_url, auth_headers = target
        return DeepgramBackend(
            model=config.deepgram_model,
            listen_url=listen_url,
            auth_headers=auth_headers,
        )

    from vetromar.transcription.local import LocalWhisperXBackend

    return LocalWhisperXBackend(config)
