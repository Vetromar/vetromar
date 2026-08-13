"""Quick notes — the lightest way to drop knowledge into a graph.

A note is its own raw layer: the typed text becomes the episode's `raw`, and
the single unit's excerpt evidence IS that text, so the evidence gate passes
because the invariant genuinely holds — no special-casing, no gate bypass.
No AI involved anywhere on this path.
"""

from __future__ import annotations

from vetromar.ingest.generic import ingest_episode, ingest_units
from vetromar.schema import ClaimPayload, Episode, ExcerptEvidence, Unit, UnitDraft
from vetromar.store import Store

# Note titles auto-derive from the first line; keep them scannable in lists.
_TITLE_MAX = 80


def _derive_title(text: str) -> str:
    first_line = text.strip().splitlines()[0].strip()
    if len(first_line) <= _TITLE_MAX:
        return first_line
    return first_line[: _TITLE_MAX - 1].rstrip() + "…"


def add_quick_note(
    store: Store,
    text: str,
    *,
    title: str | None = None,
) -> tuple[Episode, Unit]:
    """Store a note as one episode (raw = the note) + one claim unit whose
    evidence is the full note text. Returns (episode, unit)."""
    text = text.strip()
    if not text:
        raise ValueError("note text must not be empty")
    episode = ingest_episode(
        store,
        title=title.strip() if title and title.strip() else _derive_title(text),
        source_kind="note",
        raw=text,
    )
    draft = UnitDraft(
        content=_derive_title(text),
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=text)],
    )
    units = ingest_units(store, episode.id, [draft], method="concierge")
    return episode, units[0]
