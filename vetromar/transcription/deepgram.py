"""The fast tier: Deepgram batch STT with diarization included.

One synchronous POST of the raw audio bytes to /v1/listen — no SDK, stdlib
urllib only (house pattern: runtime/binary.py). Diarized utterances map
straight onto TranscriptSegment: Deepgram's integer speaker ids become the
same zero-padded `SPEAKER_NN` labels pyannote emits, so the frozen quote gate
and the auto-linking SPEAKER-skip rule see an identical transcript shape.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from vetromar.errors import ConfigError
from vetromar.schema import Transcript, TranscriptSegment
from vetromar.transcription.base import ProgressFn, TranscriptionBackend

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"

# One blocking request covers upload + processing; batch runs much faster than
# realtime, so this bounds pathological hangs, not normal operation.
REQUEST_TIMEOUT_S = 900


def _request(url: str, body: bytes, headers: dict[str, str], timeout: float) -> dict:
    """POST and parse the JSON response. Module-level so tests stub one seam."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
        return json.loads(resp.read().decode("utf-8"))


class DeepgramBackend(TranscriptionBackend):
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "nova-3",
        listen_url: str | None = None,
        auth_headers: dict[str, str] | None = None,
    ):
        self.model = model
        # The user's own Deepgram key (env DEEPGRAM_API_KEY > credentials
        # file) talks to Deepgram directly.
        if auth_headers is None:
            if not api_key:
                raise ConfigError(
                    "No Deepgram API key found for cloud transcription.",
                    hint="Add one in Settings, set DEEPGRAM_API_KEY, "
                    "or set VETROMAR_TRANSCRIBE=local.",
                )
            auth_headers = {"Authorization": f"Token {api_key}"}
        self._listen_url = listen_url or DEEPGRAM_LISTEN_URL
        self._auth_headers = auth_headers

    def transcribe(
        self, audio_path: str | Path, progress: ProgressFn | None = None
    ) -> Transcript:
        report: ProgressFn = progress or (lambda stage, pct: None)
        audio_path = Path(audio_path)

        report("Uploading audio", None)
        body = audio_path.read_bytes()
        content_type = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
        params = urllib.parse.urlencode(
            {
                "model": self.model,
                "smart_format": "true",
                "diarize": "true",
                "utterances": "true",
            }
        )
        headers = {**self._auth_headers, "Content-Type": content_type}

        report("Transcribing in cloud (Deepgram)", None)
        try:
            data = _request(
                f"{self._listen_url}?{params}", body, headers, REQUEST_TIMEOUT_S
            )
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ConfigError(
                f"Could not reach the transcription service ({exc}).",
                hint="Check your connection, or set VETROMAR_TRANSCRIBE=local.",
            ) from exc

        # Same construction as the local mapping in capture/transcribe.py:
        # SPEAKER_NN labels, int ms spans, empty-text utterances dropped.
        segments = []
        for utt in data.get("results", {}).get("utterances", []) or []:
            text = utt.get("transcript", "").strip()
            if not text:
                continue
            speaker = utt.get("speaker")
            segments.append(
                TranscriptSegment(
                    speaker=f"SPEAKER_{int(speaker):02d}"
                    if speaker is not None
                    else "SPEAKER_UNKNOWN",
                    text=text,
                    start_ms=int(utt["start"] * 1000),
                    end_ms=int(utt["end"] * 1000),
                )
            )
        return Transcript(segments=segments)

    def _map_http_error(self, exc: urllib.error.HTTPError) -> Exception:
        if exc.code in (401, 403):
            return ConfigError(
                "Deepgram rejected the API key.",
                hint="Update the key in Settings, or check DEEPGRAM_API_KEY.",
            )
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return RuntimeError(f"Deepgram request failed (HTTP {exc.code}): {detail}")
