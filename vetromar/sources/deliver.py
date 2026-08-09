"""Idempotent episode delivery — the seam between sync and the store.

Re-delivery is the normal case for a pull loop (overlapping windows, retries,
threads that keep growing), so dedup lives here, not in every caller:
- same external_id + identical content -> the existing episode, created=False
  (the caller skips extraction — nothing new happened);
- same external_id + different content (a thread grew, a message was edited)
  -> a NEW episode whose external_id is suffixed with the content hash
  (`...@<sha8>`). History is never mutated; the revision chain stays visible
  in the store. (Provisional semantics — revisit with real sync data.)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from vetromar.ingest.generic import ingest_episode
from vetromar.schema import Episode
from vetromar.store import Store


@dataclass
class DeliveryResult:
    episode: Episode
    created: bool


def _content_hash(raw: str) -> str:
    normalized = " ".join(raw.split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def deliver_episode(
    store: Store,
    *,
    title: str,
    source_kind: str,
    raw: str,
    external_id: str,
    occurred_at: datetime | None = None,
) -> DeliveryResult:
    digest = _content_hash(raw)
    for candidate_id in (external_id, f"{external_id}@{digest[:8]}"):
        existing = store.get_episode_by_external_id(candidate_id)
        if existing is None:
            continue
        if existing.raw is not None and _content_hash(existing.raw) == digest:
            return DeliveryResult(episode=existing, created=False)
        if candidate_id == external_id:
            # Same source object, different content: land it as a revision.
            external_id = f"{external_id}@{digest[:8]}"
    episode = ingest_episode(
        store,
        title=title,
        source_kind=source_kind,
        raw=raw,
        occurred_at=occurred_at,
        external_id=external_id,
    )
    return DeliveryResult(episode=episode, created=True)
