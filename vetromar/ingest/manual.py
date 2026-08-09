"""Concierge hand-entry — the human write path.

A human can be the connection layer: read the
tickets/threads, hand-enter source units, hand-link entities across systems,
and hand-link room units to the source units they spawned. Per the handoff,
this work must land in the REAL store as REAL units — same record type as
room capture, distinguished only by provenance.

Discipline boundary: these functions accept ONE record at a time, from a
human's hands. Nothing that reads a source system (Slack API, ticket API,
mail) may ever grow here — automated flow belongs to the generic ingestion
surface, not the concierge path.

Note the v2 invariant: every unit needs >=1 evidence item (a verbatim excerpt
for text sources). A quote-less hand-entered unit is now rejected at the
store door.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from vetromar.ingest.map import unit_from_draft, unit_from_extracted
from vetromar.schema import (
    Edge,
    Entity,
    Episode,
    ExtractedUnit,
    Unit,
    UnitDraft,
)
from vetromar.store import Store


def add_source_episode(
    store: Store,
    title: str,
    source_kind: str = "note",
    occurred_at: datetime | None = None,
    raw: str | None = None,
    raw_ref: str | None = None,
) -> Episode:
    """A source event: the Slack thread, the ticket, the doc a human read.
    Pass `raw` (the source text) whenever possible — evidence is validated
    against it."""
    return store.add_episode(
        Episode(
            source_kind=source_kind,
            title=title,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            raw=raw,
            raw_ref=raw_ref,
        )
    )


def add_unit_from_file(store: Store, episode_id: str, path: str | Path) -> Unit:
    """Hand-enter one unit from a JSON file. Accepts either shape:
    - a `UnitDraft` (has a `payload` key) — the generalized v2 form, any kind;
    - an `ExtractedUnit` (the meeting extractor's fields) — mapped to a
      decision unit, kept for concierge muscle memory."""
    from pydantic import ValidationError

    from vetromar.errors import ConfigError

    try:
        data = json.loads(Path(path).read_text())
        if "payload" in data:
            return add_draft(store, episode_id, UnitDraft.model_validate(data))
        return add_unit(store, episode_id, ExtractedUnit.model_validate(data))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ConfigError(
            f"{path} isn't a valid unit file: {exc}",
            hint="Expected UnitDraft JSON ({content, payload{kind,...}, evidence[...]}) "
            "or ExtractedUnit JSON (the meeting extractor's fields).",
        ) from exc


def add_unit(
    store: Store,
    episode_id: str,
    extracted: ExtractedUnit,
    valid_from: datetime | None = None,
) -> Unit:
    episode = store.get_episode(episode_id)
    unit = unit_from_extracted(
        extracted,
        episode_id=episode.id,
        method="concierge",
        valid_from=valid_from or episode.occurred_at,
    )
    return store.add_unit(unit)


def add_draft(
    store: Store,
    episode_id: str,
    draft: UnitDraft,
    valid_from: datetime | None = None,
) -> Unit:
    episode = store.get_episode(episode_id)
    unit = unit_from_draft(
        draft,
        episode_id=episode.id,
        method="concierge",
        valid_from=valid_from or episode.occurred_at,
    )
    return store.add_unit(unit)


def create_entity(store: Store, name: str, type: str = "person") -> Entity:
    return store.add_entity(Entity(name=name, type=type))


def link_alias(store: Store, entity_id: str, ref: str) -> Entity:
    """'Priya-in-Slack is Priya-in-the-standup' — one ref at a time, by hand."""
    return store.add_alias(entity_id, ref)


def link_units(store: Store, from_unit: str, to_unit: str, kind: str = "related") -> Edge:
    """The fusion edge — e.g. a room decision -> the ticket it spawned."""
    return store.add_edge(from_unit, to_unit, kind, method="manual")


def supersede(store: Store, old_unit: str, new_unit: str) -> Unit:
    """Manual bi-temporal supersession: a later decision reverses an earlier
    one. Also records the supersedes edge so the graph carries the reversal."""
    closed = store.supersede(old_unit, new_unit)
    store.add_edge(new_unit, old_unit, kind="supersedes", method="manual")
    return closed
