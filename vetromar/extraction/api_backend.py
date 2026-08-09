"""API-mode extraction: frontier model via structured outputs.

For customers comfortable with data leaving their environment; highest
quality ceiling. The provider (native Anthropic or any OpenAI-compatible
endpoint) validates the response against the shared ExtractionResult
schema — the same Pydantic models that drive the local backend's Ollama
`format` constraint.
"""

from __future__ import annotations

from vetromar.extraction.base import ExtractionBackend
from vetromar.extraction.prompt import SYSTEM_PROMPT, build_user_prompt
from vetromar.schema import ExtractedUnit, ExtractionResult, Transcript


class ApiBackend(ExtractionBackend):
    def __init__(self, config):
        self.model = config.api_model
        self._config = config
        # Access resolution (provider choice, keys, friendly sign-in errors)
        # lives in vetromar.ai — the one construction seam.
        from vetromar.ai import get_provider

        self._provider = get_provider(config)

    def extract(self, transcript: Transcript) -> list[ExtractedUnit]:
        from vetromar.ai import map_ai_error

        try:
            result: ExtractionResult = self._provider.parse_structured(
                system=SYSTEM_PROMPT,
                user=build_user_prompt(transcript.to_prompt_text()),
                schema=ExtractionResult,
                max_tokens=16000,
            )
        except Exception as exc:
            mapped = map_ai_error(exc, self._config)
            if mapped is not None:
                raise mapped from exc
            raise
        return result.units
