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
    passes none and still stops on Ctrl-C or `duration_s`.

    Audio streams to disk as it arrives — a meeting-length recording must
    never grow an in-RAM buffer for hours."""
    import queue

    import sounddevice as sd
    import soundfile as sf

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    chunks: queue.Queue = queue.Queue()

    def _callback(indata, frames, time_info, status):  # noqa: ANN001
        chunks.put(indata.copy())

    wrote_any = False
    stream = sd.InputStream(samplerate=samplerate, channels=1, callback=_callback)
    with sf.SoundFile(
        str(out_path), mode="w", samplerate=samplerate, channels=1, subtype="PCM_16"
    ) as out:

        def _drain() -> None:
            nonlocal wrote_any
            while True:
                try:
                    chunk = chunks.get_nowait()
                except queue.Empty:
                    return
                out.write(chunk)
                wrote_any = True

        try:
            with stream:
                if duration_s is not None:
                    import time

                    deadline = time.monotonic() + duration_s
                    while time.monotonic() < deadline:
                        sd.sleep(100)
                        _drain()
                elif stop_event is not None:
                    while not stop_event.is_set():
                        sd.sleep(100)
                        _drain()
                else:
                    print("Recording... press Ctrl-C to stop.")
                    while True:
                        sd.sleep(1000)
                        _drain()
        except KeyboardInterrupt:
            pass
        _drain()

    if not wrote_any:
        out_path.unlink(missing_ok=True)
        raise RuntimeError("no audio captured")
    return out_path
