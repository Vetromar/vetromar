"""Optional cross-encoder reranker — local-first, no API key, no torch.

fastembed's ONNX cross-encoder (ms-marco MiniLM, ~80 MB one-time download
cached under `~/.vetromar/rerank-model`) re-scores the fused candidate list
against the query text. Off by default (`rerank_enabled` config): it buys
precision on big stores at the cost of per-query latency. Mirrors
embedder.py: process-global lazy singleton, load failure remembered, callers
degrade to the fused order — search never fails because reranking did.
"""

from __future__ import annotations

import threading
from typing import Optional

from vetromar.config import VETROMAR_HOME

RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_CACHE_DIR = VETROMAR_HOME / "rerank-model"

_lock = threading.Lock()
_reranker = None
_unavailable_reason: Optional[str] = None


class RerankerUnavailableError(RuntimeError):
    """The rerank model can't be loaded (usually: first use while offline)."""


def get_reranker():
    global _reranker, _unavailable_reason
    with _lock:
        if _reranker is not None:
            return _reranker
        if _unavailable_reason is not None:
            raise RerankerUnavailableError(_unavailable_reason)
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            _reranker = TextCrossEncoder(RERANK_MODEL, cache_dir=str(RERANK_CACHE_DIR))
        except Exception as exc:  # noqa: BLE001 — any load failure means "degrade"
            _unavailable_reason = f"rerank model unavailable: {exc}"
            raise RerankerUnavailableError(_unavailable_reason) from exc
        return _reranker


def rerank(query: str, passages: list[str]) -> list[float]:
    """Relevance score per passage (higher = better), query-conditioned."""
    model = get_reranker()
    with _lock:  # one shared ONNX session; serialize inference across threads
        return [float(s) for s in model.rerank(query, passages)]
