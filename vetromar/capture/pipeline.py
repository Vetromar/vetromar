"""The capture pipeline — stages 1 through 6, orchestrated.

audio -> transcript (diarized, pluggable backend) -> extraction (pluggable backend)
      -> grounded-quote validation (hard gate) -> store (room ingest)
      -> markdown view (secondary output)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from vetromar.capture.audio import import_audio
from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction.base import make_backend
from vetromar.extraction.repair import heal_grounded_quotes
from vetromar.extraction.validate import validate_grounded_quotes
from vetromar.ingest.room import ingest_room
from vetromar.linking import auto_link
from vetromar.render.markdown import render_units
from vetromar.schema import Episode, Transcript, Unit
from vetromar.store import Store
from vetromar.transcription.base import make_transcription_backend


def run_pipeline(
    audio_path: str | Path,
    title: str,
    config: Config,
    store: Store,
    occurred_at: datetime | None = None,
    progress=None,
) -> tuple[Episode, list[Unit], str]:
    """Full capture: returns (episode, units written to store, markdown view).

    `progress(stage, percent)` (optional) is called through each stage so a UI
    can show real progress — transcription is the long one and reports percent."""
    audio = import_audio(audio_path)
    transcript = make_transcription_backend(config).transcribe(audio, progress=progress)
    _require_speech(transcript)

    # Persist the transcript next to the store; the episode's raw_ref points at it.
    transcript_path = config.db_path.parent / "transcripts" / f"{audio.stem}.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(transcript.model_dump_json(indent=2))

    return run_from_transcript(
        transcript,
        title=title,
        config=config,
        store=store,
        occurred_at=occurred_at,
        raw_ref=str(transcript_path),
        progress=progress,
    )


def _require_speech(transcript: Transcript) -> None:
    """Fail a capture of silence with a clear message instead of running
    extraction on nothing (which surfaces as an opaque provider error)."""
    if not transcript.segments:
        raise ConfigError(
            "No speech was detected in the recording, so there is nothing "
            "to extract.",
            hint="If people did talk, check the Microphone (and for meetings, "
            "System Audio Recording) permission in System Settings → "
            "Privacy & Security.",
        )


def run_from_transcript(
    transcript: Transcript,
    title: str,
    config: Config,
    store: Store,
    occurred_at: datetime | None = None,
    raw_ref: str | None = None,
    progress=None,
) -> tuple[Episode, list[Unit], str]:
    """Stages 4-6 only — also the entry point for transcript-file import
    and for testing extraction against the messy fixtures."""
    report = progress or (lambda stage, pct: None)
    report("Extracting decisions", None)
    backend = make_backend(config)
    extracted = backend.extract(transcript)

    # Near-miss quotes snap to their literal transcript span BEFORE the gate
    # (cheap-model tolerance; the invariant itself is untouched).
    heal_grounded_quotes(extracted, transcript)
    # Hard gate: paraphrased quotes fail loudly, never reach the store.
    validate_grounded_quotes(extracted, transcript)
    report("Saving to store", None)

    episode, units = ingest_room(
        store,
        extracted,
        title=title,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        raw=transcript.model_dump_json(),
        raw_ref=raw_ref,
    )

    # Best-effort by design: auto_link (embeddings, entity mentions,
    # relatedness/supersede edges) never raises — a linking hiccup must not
    # fail a capture whose units are already safely stored.
    report("Linking knowledge", None)
    auto_link(store, units, config)

    markdown = render_units(episode, units)
    return episode, units, markdown


def load_transcript_file(path: str | Path) -> Transcript:
    """Load a saved transcript JSON (pipeline output or test fixture)."""
    data = json.loads(Path(path).read_text())
    return Transcript.model_validate(data)
