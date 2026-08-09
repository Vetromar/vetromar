"""Room ingestion: land a captured meeting's units in the store as
first-class records — a 'meeting' Episode carrying the raw transcript, plus
decision-typed Units with 'captured' provenance."""

from __future__ import annotations

from datetime import datetime

from vetromar.ingest.map import unit_from_extracted
from vetromar.schema import Episode, ExtractedUnit, Unit
from vetromar.store import Store


def ingest_room(
    store: Store,
    extracted: list[ExtractedUnit],
    title: str,
    occurred_at: datetime,
    raw: str | None = None,
    raw_ref: str | None = None,
) -> tuple[Episode, list[Unit]]:
    episode = store.add_episode(
        Episode(
            source_kind="meeting",
            title=title,
            occurred_at=occurred_at,
            raw=raw,  # the transcript JSON — the store gate validates quotes against it
            raw_ref=raw_ref,
        )
    )
    units = store.add_units(
        [
            unit_from_extracted(
                eu,
                episode_id=episode.id,
                method="captured",
                valid_from=occurred_at,  # the fact holds from when the room decided it
            )
            for eu in extracted
        ]
    )
    return episode, units
