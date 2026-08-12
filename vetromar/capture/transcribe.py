"""Stages 2-3: transcribe + diarize via WhisperX.

WhisperX bundles Whisper ASR, word-level alignment, and pyannote's
`speaker-diarization-community-1` pipeline behind one API. pyannote was
chosen over Sortformer deliberately: no speaker cap. Our ICP's meetings
routinely exceed 4 speakers, and Sortformer's hard 4-cap silently corrupts
attribution — which is the moat.

Requires the `capture` extra and an HF token (for pyannote model download).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from vetromar.config import Config
from vetromar.schema import Transcript, TranscriptSegment

# A progress sink: (stage label, percent-of-stage or None for indeterminate).
ProgressFn = Callable[[str, Optional[float]], None]


def _asr_and_align(audio_path: str, config: Config, report: ProgressFn):
    """The shared ASR + word-alignment stages. Returns (result, audio, device)
    ready for diarization — or for direct segment mapping when the speaker is
    already known (meeting channels)."""
    try:
        import whisperx
    except ImportError as exc:
        raise RuntimeError(
            "whisperx is not installed — install the capture extra: pip install 'vetromar[capture]'"
        ) from exc

    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    # ASR — the long stage on CPU; whisperx reports per-batch progress.
    report("Loading transcription model", None)
    model = whisperx.load_model(config.whisper_model, device, compute_type=compute_type)
    audio = whisperx.load_audio(audio_path)
    report("Transcribing audio", 0.0)
    result = model.transcribe(
        audio, batch_size=8, progress_callback=lambda pct: report("Transcribing audio", pct)
    )

    # Word-level alignment (improves timestamp fidelity for grounded quotes)
    report("Aligning words", None)
    align_model, metadata = whisperx.load_align_model(
        language_code=result["language"], device=device
    )
    result = whisperx.align(result["segments"], align_model, metadata, audio, device)
    return result, audio, device


def transcribe_only(
    audio_path: str | Path,
    config: Config,
    speaker_label: str,
    progress: ProgressFn | None = None,
) -> list[TranscriptSegment]:
    """ASR + alignment with a fixed speaker — the meeting-channel path, where
    the channel already identifies the party and pyannote has nothing to add."""
    report: ProgressFn = progress or (lambda stage, pct: None)
    result, _audio, _device = _asr_and_align(str(audio_path), config, report)
    return [
        TranscriptSegment(
            speaker=speaker_label,
            text=seg["text"].strip(),
            start_ms=int(seg["start"] * 1000),
            end_ms=int(seg["end"] * 1000),
        )
        for seg in result["segments"]
        if seg.get("text", "").strip()
    ]


def transcribe_and_diarize(
    audio_path: str | Path, config: Config, progress: ProgressFn | None = None
) -> Transcript:
    report: ProgressFn = progress or (lambda stage, pct: None)
    result, audio, device = _asr_and_align(str(audio_path), config, report)
    import whisperx

    report("Identifying speakers", None)

    # Diarization — pyannote, no speaker cap. Default model is an ungated
    # CC-BY-4.0 mirror, so no HF account or token is needed; HF_TOKEN only
    # matters if config points at a gated repo.
    try:
        diarize_model = whisperx.diarize.DiarizationPipeline(
            model_name=config.diarization_model, token=config.hf_token, device=device
        )
    except Exception as exc:
        raise RuntimeError(
            f"could not load diarization model {config.diarization_model!r}. "
            "The default is ungated and needs no credentials — if you overrode "
            "VETROMAR_DIARIZATION_MODEL to a gated repo, accept its terms on "
            "Hugging Face and set HF_TOKEN. (Customers never need this: shipped "
            "builds bundle the weights.)"
        ) from exc
    diarize_segments = diarize_model(audio)
    result = whisperx.assign_word_speakers(diarize_segments, result)

    segments = [
        TranscriptSegment(
            speaker=seg.get("speaker", "SPEAKER_UNKNOWN"),
            text=seg["text"].strip(),
            start_ms=int(seg["start"] * 1000),
            end_ms=int(seg["end"] * 1000),
        )
        for seg in result["segments"]
        if seg.get("text", "").strip()
    ]
    return Transcript(segments=segments)
