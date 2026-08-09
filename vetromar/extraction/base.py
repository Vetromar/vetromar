"""The pluggable extraction seam — the privacy-choice boundary.

One interface, two interchangeable backends selected by configuration:
- ApiBackend  (frontier model via Anthropic structured output)
- LocalBackend (Ollama with schema-constrained JSON; data never leaves the machine)

Everything upstream (audio, transcript, diarization) and downstream (store,
ingest, render, MCP) is identical regardless of which backend produced the
units. Do not let backend-specific behavior leak past this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from vetromar.schema import ExtractedUnit, Transcript


class ExtractionBackend(ABC):
    @abstractmethod
    def extract(self, transcript: Transcript) -> list[ExtractedUnit]:
        """Turn a diarized transcript into structured decision units.

        Implementations must guarantee STRUCTURE (schema-valid output) at
        this boundary; content quality is what M3 iterates on."""


def make_backend(config) -> ExtractionBackend:
    """Backend factory driven by config — the customer's privacy choice."""
    if config.backend == "api":
        from vetromar.extraction.api_backend import ApiBackend

        return ApiBackend(config)
    if config.backend == "local":
        from vetromar.extraction.local_backend import LocalBackend

        return LocalBackend(model=config.local_model, seed=config.local_seed)
    raise ValueError(f"unknown extraction backend: {config.backend!r} (expected 'api' or 'local')")
