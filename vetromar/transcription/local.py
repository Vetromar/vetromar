"""The private tier: whisperx + pyannote on this machine, wrapped unchanged.

`capture/transcribe.py` keeps the actual pipeline (and its load-bearing
SPEAKER mapping); this class only adapts it to the TranscriptionBackend
interface.
"""

from __future__ import annotations

from pathlib import Path

from vetromar.capture.transcribe import transcribe_and_diarize
from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.schema import Transcript
from vetromar.transcription.base import ProgressFn, TranscriptionBackend


class LocalWhisperXBackend(TranscriptionBackend):
    def __init__(self, config: Config):
        self._config = config

    def ensure_models_present(self) -> None:
        # Model downloads are explicit (Settings → Download local models) —
        # never let a capture kick off a silent multi-GB fetch. Non-English
        # alignment models are the accepted exception: they still lazy-load
        # inside whisperx (blocking them would break non-English capture).
        from vetromar.transcription.assets import (
            missing_component_names,
            transcription_models_status,
        )

        status = transcription_models_status(self._config)
        if not status["present"]:
            missing = ", ".join(missing_component_names(status))
            raise ConfigError(
                f"Local transcription models aren't downloaded yet (missing: {missing}).",
                hint="Open Settings → Download local models (~8 GB), or subscribe "
                "to use cloud transcription.",
            )

    def transcribe(
        self, audio_path: str | Path, progress: ProgressFn | None = None
    ) -> Transcript:
        self.ensure_models_present()
        return transcribe_and_diarize(audio_path, self._config, progress=progress)
