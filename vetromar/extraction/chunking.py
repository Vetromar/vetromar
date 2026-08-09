"""Deterministic chunking for generic extraction over large sources.

Every chunk is a VERBATIM slice `text[start:end]` of the original — never a
re-join — so any excerpt a model copies from a chunk is automatically a
literal span of the full episode raw and the evidence gate is untouched.
Chunk boundaries prefer paragraph breaks, then line breaks, then a hard cut;
consecutive chunks overlap so a claim straddling a boundary appears whole in
at least one chunk (the resulting duplicate units are deduped downstream).

No tokenizer dependency: character budgets keep this deterministic and
cheap-model friendly (~16k chars ≈ 4k tokens of source per call).
"""

from __future__ import annotations

import re

CHUNK_CHARS = 16_000     # target chunk size
CHUNK_OVERLAP = 1_000    # tail of each chunk repeated at the head of the next
SINGLE_CALL_LIMIT = 20_000  # raw at or below this keeps the historic one-call path

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def chunk_text(
    text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Split `text` into overlapping verbatim slices of at most `size` chars.

    Boundary choice per chunk: the last paragraph break in the back half of
    the window, else the last newline in the back half, else a hard cut at
    the window edge (the back-half constraint bounds the chunk count at
    ~2·len/size even for break-heavy text)."""
    if len(text) <= size:
        return [text]
    breaks = [m.end() for m in _PARAGRAPH_BREAK.finditer(text)]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        limit = start + size
        if limit >= len(text):
            chunks.append(text[start:])
            break
        end = _best_break(text, breaks, lo=start + size // 2, hi=limit)
        chunks.append(text[start:end])
        # Overlap the tail so boundary-straddling content appears whole in
        # the next chunk; guard against non-advancing starts.
        start = end - overlap if end - overlap > start else end
    return chunks


def _best_break(text: str, breaks: list[int], lo: int, hi: int) -> int:
    import bisect

    idx = bisect.bisect_right(breaks, hi) - 1
    if idx >= 0 and breaks[idx] > lo:
        return breaks[idx]
    newline = text.rfind("\n", lo, hi)
    if newline > lo:
        return newline + 1
    return hi
