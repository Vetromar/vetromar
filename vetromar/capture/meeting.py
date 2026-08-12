"""Virtual-meeting capture: mic + system-audio process tap (macOS 14.2+).

The native side lives in the bundled `vetromar-helper` binary (Swift); this
module only spawns and supervises it. A meeting recording is two parallel
16 kHz mono streams — the microphone (you) via sounddevice, the meeting
app's output (everyone else) via a Core Audio process tap — composed at stop
into one stereo WAV: L = mic, R = system. Everything downstream sees a
normal Transcript, so the evidence gate and frozen surfaces are untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from vetromar.capture.audio import record_mic
from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.schema import Episode, Unit
from vetromar.store import Store

# Watched meeting sources: (bundle-id prefix, display label). Prefix matching
# covers helper processes (Chromium browsers do audio I/O in `<id>.helper*`
# children; Safari's media runs in the WebKit GPU process).
WATCHED_APPS: list[tuple[str, str]] = [
    ("us.zoom.xos", "Zoom"),
    ("com.microsoft.teams2", "Microsoft Teams"),
    ("com.microsoft.teams", "Microsoft Teams"),
    ("com.google.Chrome", "Chrome"),
    ("com.apple.WebKit.GPU", "Safari"),
    ("company.thebrowser.Browser", "Arc"),
    ("com.microsoft.edgemac", "Edge"),
]

# The stereo channel -> speaker contract shared with transcription/meeting.py.
MIC_SPEAKER = "SPEAKER_00"
SYSTEM_SPEAKER = "SPEAKER_01"

_HELPER_STOP_TIMEOUT_S = 10


def watched_prefixes() -> list[str]:
    return [prefix for prefix, _ in WATCHED_APPS]


def app_label(bundle_prefix: str) -> str:
    for prefix, label in WATCHED_APPS:
        if prefix == bundle_prefix:
            return label
    return bundle_prefix


def find_helper() -> Path | None:
    """Locate the native capture helper: env override > next to the frozen
    sidecar binary > the dev build in the repo checkout."""
    env = os.environ.get("VETROMAR_AUDIO_HELPER")
    if env:
        path = Path(env)
        return path if path.exists() else None
    frozen = Path(sys.executable).resolve().parent / "vetromar-helper"
    if frozen.exists():
        return frozen
    dev = Path(__file__).resolve().parents[2] / "desktop" / "helper" / "vetromar-helper"
    if dev.exists():
        return dev
    return None


def helper_selftest(helper: Path) -> bool:
    """Whether the helper can see Core Audio process objects on this machine."""
    try:
        proc = subprocess.run(
            [str(helper), "selftest"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def record_meeting(
    out_dir: str | Path,
    bundle_prefix: str,
    stop_event: threading.Event,
    on_log: Optional[Callable[[str], None]] = None,
) -> Path:
    """Record mic + the meeting app's system audio until `stop_event`.

    Returns the composed stereo WAV (L = mic, R = system). If the tap yielded
    only silence (system-audio permission not yet granted) the recording
    degrades to the mono mic file with a log line; if BOTH channels are empty
    it raises, naming the permission."""
    log = on_log or (lambda message: None)
    out_dir = Path(out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mic_path = out_dir / f"{stamp}-mic.wav"
    system_path = out_dir / f"{stamp}-system.wav"
    stereo_path = out_dir / f"{stamp}-meeting.wav"

    helper = find_helper()
    if helper is None:
        raise ConfigError(
            "The system-audio capture helper is missing.",
            hint="Meeting capture needs the desktop app build (vetromar-helper).",
        )

    # The helper exits when its stdin closes — parent-death safety. Timestamps
    # around each recorder's start align the two channels at compose time
    # (rough alignment is fine; transcription merges by segment spans).
    tap_started = {"at": None}
    proc = subprocess.Popen(
        [str(helper), "tap", "--bundle-prefix", bundle_prefix, "--out", str(system_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
    )

    def _watch_helper() -> None:
        for line in proc.stdout or []:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "tapping" and tap_started["at"] is None:
                tap_started["at"] = time.monotonic()
            elif event.get("event") == "error":
                log(f"system-audio helper error: {event.get('message')}")

    watcher = threading.Thread(target=_watch_helper, daemon=True)
    watcher.start()

    mic_started = time.monotonic()
    mic_error: Exception | None = None
    try:
        record_mic(mic_path, stop_event=stop_event, samplerate=16000)
    except Exception as exc:  # keep the tap's side even if the mic fails
        mic_error = exc
        log(f"microphone capture failed: {exc}")
        stop_event.wait()

    # Stop the tap: stdin close is the signal; SIGTERM is the backstop.
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=_HELPER_STOP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
    watcher.join(timeout=2)

    mic_ok = mic_error is None and _has_audio(mic_path)
    system_ok = _has_audio(system_path)
    if not mic_ok and not system_ok:
        raise ConfigError(
            "The meeting recording captured no audio on either channel.",
            hint="Check Microphone and System Audio Recording permissions for "
            "Vetromar in System Settings → Privacy & Security.",
        )
    if not system_ok:
        log(
            "system audio was silent (permission pending?) — this capture is "
            "microphone-only"
        )
        system_path.unlink(missing_ok=True)
        return mic_path
    if not mic_ok:
        log("microphone channel was empty — this capture is system-audio-only")
        mic_path.unlink(missing_ok=True)
        return system_path

    # Positive offset = the system channel started later than the mic.
    offset_s = (tap_started["at"] or mic_started) - mic_started
    compose_stereo(mic_path, system_path, stereo_path, system_offset_s=offset_s)
    mic_path.unlink(missing_ok=True)
    system_path.unlink(missing_ok=True)
    return stereo_path


def _has_audio(path: Path) -> bool:
    """True when the WAV exists and holds at least one non-zero sample."""
    import numpy as np
    import soundfile as sf

    if not path.exists():
        return False
    try:
        with sf.SoundFile(str(path)) as handle:
            while True:
                block = handle.read(65536, dtype="int16", always_2d=True)
                if len(block) == 0:
                    return False
                if np.any(block):
                    return True
    except RuntimeError:
        return False


def compose_stereo(
    mic_path: Path,
    system_path: Path,
    out_path: Path,
    system_offset_s: float = 0.0,
    samplerate: int = 16000,
) -> Path:
    """Interleave the two mono channels into one stereo WAV (L=mic, R=system),
    padding the later-starting channel with leading silence. Blockwise — never
    a whole meeting in RAM."""
    import numpy as np
    import soundfile as sf

    offset_frames = int(round(abs(system_offset_s) * samplerate))
    # The later-starting channel gets the leading silence.
    system_pad = offset_frames if system_offset_s > 0 else 0
    mic_pad = offset_frames if system_offset_s < 0 else 0

    block_size = 65536
    with (
        sf.SoundFile(str(mic_path)) as mic,
        sf.SoundFile(str(system_path)) as system,
        sf.SoundFile(
            str(out_path), mode="w", samplerate=samplerate, channels=2, subtype="PCM_16"
        ) as out,
    ):
        while True:
            mic_block = _padded_read(mic, mic_pad, block_size)
            system_block = _padded_read(system, system_pad, block_size)
            mic_pad = max(0, mic_pad - block_size)
            system_pad = max(0, system_pad - block_size)
            frames = max(len(mic_block), len(system_block))
            if frames == 0:
                break
            stereo = np.zeros((frames, 2), dtype="int16")
            stereo[: len(mic_block), 0] = mic_block[:, 0]
            stereo[: len(system_block), 1] = system_block[:, 0]
            out.write(stereo)
    return out_path


def _padded_read(handle, lead_frames: int, block_size: int):
    """Read a block, consuming up to `block_size` frames of leading silence
    first (the alignment pad for the later-starting channel)."""
    import numpy as np

    if lead_frames >= block_size:
        return np.zeros((block_size, 1), dtype="int16")
    pad = np.zeros((lead_frames, 1), dtype="int16") if lead_frames else None
    data = handle.read(block_size - lead_frames, dtype="int16", always_2d=True)
    if pad is None:
        return data
    return np.concatenate([pad, data], axis=0)


def run_meeting_pipeline(
    audio_path: str | Path,
    title: str,
    config: Config,
    store: Store,
    occurred_at: datetime | None = None,
    progress=None,
) -> tuple[Episode, list[Unit], str]:
    """The meeting variant of capture.pipeline.run_pipeline: channel-aware
    transcription (stereo -> you/others), then the unchanged stages 4-6."""
    from vetromar.capture.pipeline import _require_speech, run_from_transcript
    from vetromar.transcription.meeting import transcribe_meeting

    audio_path = Path(audio_path)
    transcript = transcribe_meeting(audio_path, config, progress=progress)
    _require_speech(transcript)

    transcript_path = config.db_path.parent / "transcripts" / f"{audio_path.stem}.json"
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
