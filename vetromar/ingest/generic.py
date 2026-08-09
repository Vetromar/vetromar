"""The generic ingestion surface — source-agnostic, connector-free.

Anything (an agent over MCP, the CLI, future integrations) can land knowledge
here in two motions:
1. `ingest_episode` — the RAW layer: the source content itself, verbatim.
2. `ingest_units` — the INTERPRETED layer: typed UnitDrafts whose evidence is
   validated against that raw content at the store door (atomic batch).

No per-source connector code exists or may grow here: a source Vetromar has
never seen ingests exactly like one it has, because the caller brings the
content and the engine only ever sees (raw text, typed claims, evidence).
"""

from __future__ import annotations

from datetime import datetime, timezone

from vetromar.config import Config
from vetromar.ingest.map import unit_from_draft
from vetromar.linking import auto_link
from vetromar.schema import Episode, Unit, UnitDraft
from vetromar.store import Store


def ingest_episode(
    store: Store,
    *,
    title: str,
    source_kind: str,
    raw: str | None = None,
    raw_ref: str | None = None,
    occurred_at: datetime | None = None,
    external_id: str | None = None,
) -> Episode:
    return store.add_episode(
        Episode(
            source_kind=source_kind,
            title=title,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            raw=raw,
            raw_ref=raw_ref,
            external_id=external_id,
        )
    )


def ingest_units(
    store: Store,
    episode_id: str,
    drafts: list[UnitDraft],
    method: str = "pushed",
    agent: str | None = None,
    config: Config | None = None,
) -> list[Unit]:
    """Land a batch of drafts in one atomic, gate-enforced write: any evidence
    failure rejects the WHOLE batch with the offender named. Facts default to
    holding from when the source event occurred. Stored units are auto-linked
    (best-effort — a linking failure never fails the ingest); pass `config`
    to enable the LLM linking tiers in api mode."""
    episode = store.get_episode(episode_id)
    units = [
        unit_from_draft(
            draft,
            episode_id=episode.id,
            method=method,
            valid_from=episode.occurred_at,
            agent=agent,
        )
        for draft in drafts
    ]
    stored = store.add_units(units)
    auto_link(store, stored, config)
    return stored
