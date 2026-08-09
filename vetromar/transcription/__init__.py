"""The pluggable transcription seam — the second privacy-choice boundary.

Mirrors `vetromar/extraction/`: one interface, interchangeable backends
selected by configuration. See base.py.
"""

from vetromar.transcription.base import (
    TranscriptionBackend,
    make_transcription_backend,
    resolve_transcription_mode,
)

__all__ = [
    "TranscriptionBackend",
    "make_transcription_backend",
    "resolve_transcription_mode",
]
