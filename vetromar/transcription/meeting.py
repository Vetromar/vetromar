"""Channel-aware transcription for meeting recordings.

A meeting WAV is stereo by construction: L = the microphone (you),
R = the meeting app's system audio (everyone else). Speakers are therefore
known per channel — no diarization models involved:

    channel 0 -> SPEAKER_00 (you)      channel 1 -> SPEAKER_01 (others)

Cloud runs one Deepgram multichannel request; local splits the channels and
runs WhisperX ASR+alignment per channel (pyannote skipped — a single-party
channel has nothing to diarize). Both produce a byte-normal Transcript, so
the frozen quote gate and auto-linking see the usual shape. The existing
`TranscriptionBackend.transcribe` seam is untouched: user-uploaded stereo
files keep their current behavior; only the explicit meeting path lands here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from vetromar.config import Config
from vetromar.schema import Transcript, TranscriptSegment
from vetromar.transcription.base import (
    ProgressFn,
    make_transcription_backend,
)

MIC_SPEAKER = "SPEAKER_00"
SYSTEM_SPEAKER = "SPEAKER_01"


def transcribe_meeting(
    audio_path: str | Path, config: Config, progress: ProgressFn | None = None
) -> Transcript:
    """Transcribe a meeting recording with channel-derived speakers.

    A mono file (a capture that degraded to one channel) falls back to the
    normal diarizing backend — better one diarized channel than mislabeling
    everything as one speaker."""
    from vetromar.transcription.deepgram import DeepgramBackend

    audio_path = Path(audio_path)
    backend = make_transcription_backend(config)

    if _channel_count(audio_path) < 2:
        return backend.transcribe(audio_path, progress=progress)

    if isinstance(backend, DeepgramBackend):
        return backend.transcribe_multichannel(audio_path, progress=progress)

    # Local tier: the backend has already gated on model presence at
    # construction time in the pipeline; gate again here for direct callers.
    from vetromar.capture.transcribe import transcribe_only

    backend.ensure_models_present()
    with tempfile.TemporaryDirectory(prefix="vetromar-meeting-") as tmp:
        mic_path, system_path = split_stereo(audio_path, Path(tmp))
        mic_segments = transcribe_only(mic_path, config, MIC_SPEAKER, progress=progress)
        system_segments = transcribe_only(
            system_path, config, SYSTEM_SPEAKER, progress=progress
        )
    return merge_channel_transcripts(mic_segments, system_segments)


def merge_channel_transcripts(*channels: list[TranscriptSegment]) -> Transcript:
    """Interleave per-channel segments chronologically. Segments already carry
    their channel's fixed speaker label."""
    merged = [segment for channel in channels for segment in channel]
    merged.sort(key=lambda segment: (segment.start_ms, segment.end_ms))
    return Transcript(segments=merged)


def split_stereo(stereo_path: Path, out_dir: Path) -> tuple[Path, Path]:
    """Split a stereo WAV into (left, right) mono files, blockwise."""
    import soundfile as sf

    left_path = out_dir / f"{stereo_path.stem}-left.wav"
    right_path = out_dir / f"{stereo_path.stem}-right.wav"
    with sf.SoundFile(str(stereo_path)) as stereo:
        if stereo.channels < 2:
            raise ValueError(f"{stereo_path} is not stereo")
        with (
            sf.SoundFile(
                str(left_path),
                mode="w",
                samplerate=stereo.samplerate,
                channels=1,
                subtype="PCM_16",
            ) as left,
            sf.SoundFile(
                str(right_path),
                mode="w",
                samplerate=stereo.samplerate,
                channels=1,
                subtype="PCM_16",
            ) as right,
        ):
            while True:
                block = stereo.read(65536, dtype="int16", always_2d=True)
                if len(block) == 0:
                    break
                left.write(block[:, 0])
                right.write(block[:, 1])
    return left_path, right_path


def _channel_count(path: Path) -> int:
    import soundfile as sf

    try:
        with sf.SoundFile(str(path)) as handle:
            return handle.channels
    except RuntimeError:
        return 1
