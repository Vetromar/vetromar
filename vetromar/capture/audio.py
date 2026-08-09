"""Stage 1: acquire audio.

Primary path: import a file from an off-the-shelf recorder — we are the
layer ABOVE whatever captures the room, deliberately device-agnostic.
Secondary path: live capture from the machine mic (macOS initial target).
"""

from __future__ import annotations

from pathlib import Path

SUPPORTED_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4"}


def import_audio(path: str | Path) -> Path:
    """Validate an imported recording. WhisperX handles decoding via ffmpeg."""
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"audio file not found: {path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"unsupported audio format {path.suffix!r}; expected one of {sorted(SUPPORTED_SUFFIXES)}"
        )
    return path


def record_mic(
    out_path: str | Path,
    duration_s: float | None = None,
    samplerate: int = 16000,
    stop_event=None,
) -> Path:
    """Record from the default microphone until Ctrl-C (or duration_s).
    Secondary path — requires the `capture` extra (sounddevice/soundfile).

    `stop_event` (a `threading.Event`) is the non-interactive stop signal used
    by the desktop UI's Record button: when set, recording ends. The CLI path
    passes none and still stops on Ctrl-C or `duration_s`."""
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    chunks: list[np.ndarray] = []

    def _callback(indata, frames, time_info, status):  # noqa: ANN001
        chunks.append(indata.copy())

    stream = sd.InputStream(samplerate=samplerate, channels=1, callback=_callback)
    try:
        with stream:
            if duration_s is not None:
                sd.sleep(int(duration_s * 1000))
            elif stop_event is not None:
                while not stop_event.is_set():
                    sd.sleep(100)
            else:
                print("Recording... press Ctrl-C to stop.")
                while True:
                    sd.sleep(1000)
    except KeyboardInterrupt:
        pass

    if not chunks:
        raise RuntimeError("no audio captured")
    audio = np.concatenate(chunks, axis=0)
    sf.write(str(out_path), audio, samplerate)
    return out_path
