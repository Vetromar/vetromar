"""Embedding provider — local-first, no API key, no torch.

fastembed (ONNX) with BGE-small: a one-time ~65 MB download cached under
`~/.vetromar/embed-model`, CPU inference, and the SAME embedding space in both
extraction modes (cloud and local), so vectors never need re-computing when
the backend switches.

BGE is ASYMMETRIC: queries are embedded with an instruction prefix
(`query_embed`) while passages are embedded plain (`embed`). Mixing the two
degrades ranking — always use `embed_query` for search text and
`embed_passages` for unit text.
"""

from __future__ import annotations

import struct
import threading
from typing import Optional

from vetromar.config import VETROMAR_HOME

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_CACHE_DIR = VETROMAR_HOME / "embed-model"

_lock = threading.Lock()
_embedder = None
_unavailable_reason: Optional[str] = None


class EmbedderUnavailableError(RuntimeError):
    """The embedding model can't be loaded (usually: first use while offline).
    Search degrades to full-text-only; `vetromar doctor` reports it."""


def get_embedder():
    """Process-global lazy singleton. First call downloads the model if it
    isn't cached; a load failure is remembered for the life of the process."""
    global _embedder, _unavailable_reason
    with _lock:
        if _embedder is not None:
            return _embedder
        if _unavailable_reason is not None:
            raise EmbedderUnavailableError(_unavailable_reason)
        try:
            from fastembed import TextEmbedding

            _embedder = TextEmbedding(EMBED_MODEL, cache_dir=str(EMBED_CACHE_DIR))
        except Exception as exc:  # noqa: BLE001 — any load failure means "degrade"
            _unavailable_reason = f"embedding model unavailable: {exc}"
            raise EmbedderUnavailableError(_unavailable_reason) from exc
        return _embedder


def embed_passages(texts: list[str]) -> list[bytes]:
    """Passage vectors for unit text, as float32-LE blobs."""
    model = get_embedder()
    with _lock:  # one shared ONNX session; serialize inference across threads
        vectors = list(model.embed(texts))
    return [pack_vector(v) for v in vectors]


def embed_query(text: str) -> bytes:
    """Query vector for search text (instruction-prefixed), float32-LE blob."""
    model = get_embedder()
    with _lock:
        vector = next(iter(model.query_embed(text)))
    return pack_vector(vector)


def pack_vector(vector) -> bytes:
    values = [float(x) for x in vector]
    return struct.pack(f"<{len(values)}f", *values)


def prefetch_model() -> None:
    """Download/load the model now (setup-time nicety so the first search
    isn't the thing that pays for the download)."""
    get_embedder()


def embedder_status() -> dict:
    """Non-loading probe for doctor: is the model already cached on disk?"""
    cached = EMBED_CACHE_DIR.exists() and any(EMBED_CACHE_DIR.rglob("*.onnx"))
    return {"model": EMBED_MODEL, "cached": cached, "cache_dir": str(EMBED_CACHE_DIR)}
