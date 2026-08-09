"""Local-mode extraction: Ollama with schema-constrained JSON output.

Data never leaves the machine — the consent asset for privacy-sensitive
customers. Structure is guaranteed at the token level: Ollama's `format`
parameter takes our JSON schema and compiles it to a grammar internally
(the modern equivalent of the hand-written GBNF the brief describes), so
only CONTENT quality varies between local models, never shape.

The schema passed here is exported from the same Pydantic models that drive
the API backend — one source of truth for both modes.
"""

from __future__ import annotations

import json

from vetromar.errors import ConfigError
from vetromar.extraction.base import ExtractionBackend
from vetromar.extraction.prompt import SYSTEM_PROMPT, build_user_prompt
from vetromar.schema import ExtractedUnit, ExtractionResult, Transcript


def _is_missing_model(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "not found" in msg or "no such model" in msg or "try pulling" in msg

# Generation order for ExtractedUnit under the Ollama grammar: evidence and
# the moat fields (alternatives, advocate, objectors) come BEFORE the prose.
_UNIT_FIELD_ORDER = [
    "decision",
    "grounded_quotes",
    "rejected_alternatives",
    "advocate",
    "objectors",
    "status",
    "reasoning",
]


def _extraction_format() -> dict:
    """The Pydantic schema, tightened for grammar-constrained decoding.

    Pydantic marks defaulted fields (advocate, rejected_alternatives, ...)
    as optional, so the grammar lets a local model skip those keys entirely —
    observed as advocate/alternatives silently never emitted. Force every
    field required (null/[] must be explicit) and reorder properties so the
    model produces evidence before prose. The foundational schema itself is
    untouched; this shapes only the local decoding grammar.
    """
    schema = ExtractionResult.model_json_schema()
    unit = schema["$defs"]["ExtractedUnit"]
    unit["properties"] = {k: unit["properties"][k] for k in _UNIT_FIELD_ORDER}
    unit["required"] = _UNIT_FIELD_ORDER
    return schema


class LocalBackend(ExtractionBackend):
    def __init__(self, model: str = "qwen3.5:9b", seed: int = 42):
        self.model = model
        self.seed = seed

    def extract(self, transcript: Transcript) -> list[ExtractedUnit]:
        import ollama

        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(transcript.to_prompt_text())},
            ],
            format=_extraction_format(),
            # Low temperature (<=0.2) degenerates under the format grammar: the
            # model never closes the units array and re-emits near-duplicate
            # units until cut off. Near-default temperature terminates reliably;
            # the fixed seed keeps runs reproducible for prompt iteration.
            # num_predict caps runaway generation: a loud parse error in
            # minutes instead of a 30+ min hang.
            options={"num_ctx": 32768, "temperature": 0.6, "seed": self.seed, "num_predict": 8192},
        )
        # Provisioning failures (server down, model never pulled) become a
        # friendly ConfigError pointing at `vetromar setup`. The chat call
        # itself — args, format grammar, think=False, options — is untouched.
        try:
            try:
                # The `format` grammar constrains only the content channel, not
                # thinking — a thinking model (qwen3.5) left to think runs for
                # 10k+ unconstrained tokens before any JSON. Disable it.
                response = ollama.chat(**kwargs, think=False)
            except ollama.ResponseError as exc:
                if "thinking" not in str(exc):
                    raise
                response = ollama.chat(**kwargs)
        except ConnectionError as exc:
            raise ConfigError(
                "The local model runtime isn't reachable.",
                hint="Run `vetromar setup` to start it, or `vetromar doctor` to diagnose.",
            ) from exc
        except ollama.ResponseError as exc:
            if _is_missing_model(exc):
                raise ConfigError(
                    f"The local model {self.model!r} isn't installed.",
                    hint="Run `vetromar setup` to download it.",
                ) from exc
            raise
        raw = response["message"]["content"]
        try:
            result = ExtractionResult.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            # Should be unreachable with schema-constrained decoding; if it
            # fires, the Ollama version predates schema `format` support.
            raise RuntimeError(f"local extraction emitted non-conforming output: {exc}") from exc
        return result.units
