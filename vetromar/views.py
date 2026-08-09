"""Read views over the store, shared by the MCP server and the desktop UI API.

Same idea as `operations.py` for setup/prep: one place computes the JSON
payloads both surfaces return, so an agent over MCP and a human in the app see
the same picture. Every function takes the `Store` explicitly — callers own
connection lifetime (the MCP server keeps one, the HTTP API opens one per
request because SQLite is thread-bound).

Episode `raw` (full transcripts / source text) is excluded from payloads
unless explicitly requested — it can be huge.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from vetromar.schema import Edge, Episode, Unit
from vetromar.store import Store

_LABEL_MAX = 90


def parse_as_of(as_of: Optional[str]) -> Optional[datetime]:
    """ISO datetime string → aware datetime (naive input treated as UTC)."""
    if not as_of:
        return None
    dt = datetime.fromisoformat(as_of)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def episode_dict(episode: Episode, include_raw: bool = False) -> dict:
    d = json.loads(episode.model_dump_json())
    if not include_raw:
        d.pop("raw", None)
    return d


def unit_payload(store: Store, unit: Unit, with_labels: bool = False) -> dict:
    """A unit plus its fused context: episode provenance and its edges.

    `with_labels` (UI only — not part of the MCP shape) adds a `labels` map
    resolving every edge endpoint to a short display label, so the UI can
    render clickable edges without a follow-up request per edge."""
    episode = store.get_episode(unit.provenance.episode_id)
    edges = store.edges_for(unit.id)
    payload = {
        "unit": json.loads(unit.model_dump_json()),
        "episode": episode_dict(episode),
        "edges": [json.loads(e.model_dump_json()) for e in edges],
    }
    if with_labels:
        payload["labels"] = edge_labels(store, edges)
    return payload


def edge_labels(store: Store, edges: list[Edge]) -> dict[str, dict]:
    """Map every node id appearing in `edges` to {label, node} — entity name
    for `ent_*`, truncated unit content otherwise. Unresolvable ids (should
    not happen — edges are app-validated) fall back to the id itself."""
    labels: dict[str, dict] = {}
    for edge in edges:
        for node_id in (edge.from_id, edge.to_id):
            if node_id in labels:
                continue
            try:
                if node_id.startswith("ent_"):
                    entity = store.get_entity(node_id)
                    labels[node_id] = {"label": entity.name, "node": "entity"}
                else:
                    unit = store.get_unit(node_id)
                    text = unit.content
                    if len(text) > _LABEL_MAX:
                        text = text[: _LABEL_MAX - 1] + "…"
                    labels[node_id] = {"label": text, "node": "unit"}
            except Exception:
                labels[node_id] = {"label": node_id, "node": "unknown"}
    return labels


def search_units(
    store: Store,
    text: str | None = None,
    *,
    type: str | None = None,
    status: str | None = None,
    episode_id: str | None = None,
    method: str | None = None,
    current_only: bool = False,
    as_of: str | None = None,
    k: int = 10,
    with_labels: bool = False,
) -> list[dict]:
    """Hybrid ranked search (with `text`) or a filtered listing (without)."""
    from vetromar import search as search_mod

    as_of_dt = parse_as_of(as_of)
    if text:
        hits = [
            scored.unit
            for scored in search_mod.search(
                store,
                text,
                k=k,
                type=type,
                status=status,
                episode_id=episode_id,
                method=method,
                current_only=current_only,
                as_of=as_of_dt,
            )
        ]
    else:
        hits = store.list_units(
            type=type,
            status=status,
            episode_id=episode_id,
            method=method,
            current_only=current_only,
            as_of=as_of_dt,
        )
    return [unit_payload(store, u, with_labels=with_labels) for u in hits]


def episode_detail(store: Store, episode_id: str, include_raw: bool = False) -> dict:
    """One episode and the units it produced."""
    episode = store.get_episode(episode_id)
    units = store.list_units(episode_id=episode_id)
    return {
        "episode": episode_dict(episode, include_raw=include_raw),
        "units": [json.loads(u.model_dump_json()) for u in units],
    }


def graph(store: Store) -> dict:
    """The whole knowledge graph in one payload for the UI's graph view:
    entities, units (with evidence summaries), episodes, and every edge.
    The client assembles its abstraction layers from this — unit→episode
    links derive from `episode_id`, evidence nodes from the `evidence`
    lists. Sized for v0 stores (one team's knowledge)."""
    def _snippet(ev) -> str:
        text = getattr(ev, "text", None) or getattr(ev, "description", "") or ""
        return text[:120]

    def _status(unit: Unit):
        status = getattr(unit.payload, "status", None)
        return status.value if status is not None else None

    return {
        "entities": [
            {"id": e.id, "name": e.name, "type": e.type} for e in store.list_entities()
        ],
        "units": [
            {
                "id": u.id,
                "content": u.content,
                "type": u.type,
                "status": _status(u),
                "superseded": u.valid_to is not None,
                "episode_id": u.provenance.episode_id,
                "evidence": [
                    {"kind": ev.kind, "text": _snippet(ev)} for ev in u.evidence
                ],
            }
            for u in store.list_units()
        ],
        "episodes": [
            {"id": ep.id, "title": ep.title, "source_kind": ep.source_kind}
            for ep in store.list_episodes()
        ],
        "edges": [
            {
                "from_id": e.from_id,
                "to_id": e.to_id,
                "kind": e.kind,
                "confidence": e.confidence,
            }
            for e in store.list_edges()
        ],
    }


def _compact_unit(unit: Unit) -> dict:
    row = {
        "id": unit.id,
        "type": unit.type,
        "content": unit.content,
        "valid_from": unit.valid_from.isoformat(),
    }
    status = getattr(unit.payload, "status", None)
    if status is not None:
        row["status"] = status.value
    return row


def current_state(store: Store, entity_id: str | None = None) -> dict:
    """Every unit still valid (not superseded), grouped by the entity it
    involves — 'what is the current direction' in one call."""
    if entity_id is not None:
        entity = store.get_entity(entity_id)
        units = [u for u in store.units_by_entity(entity_id) if u.valid_to is None]
        units.sort(key=lambda u: u.ingested_at, reverse=True)
        return {
            "entities": [{"entity": json.loads(entity.model_dump_json()),
                          "units": [_compact_unit(u) for u in units]}],
            "unattributed": [],
        }

    current = store.list_units(current_only=True)
    current.sort(key=lambda u: u.ingested_at, reverse=True)
    buckets: dict[str, list[dict]] = {}
    unattributed: list[dict] = []
    for unit in current:
        entity_ids = {
            (e.from_id if e.to_id == unit.id else e.to_id)
            for e in store.edges_for(unit.id)
            if e.kind in ("mentions", "about")
        }
        entity_ids = {i for i in entity_ids if i.startswith("ent_")}
        if not entity_ids:
            unattributed.append(_compact_unit(unit))
        for eid in entity_ids:
            buckets.setdefault(eid, []).append(_compact_unit(unit))
    entities = [
        {"entity": json.loads(store.get_entity(eid).model_dump_json()), "units": rows}
        for eid, rows in buckets.items()
    ]
    entities.sort(key=lambda b: b["units"][0]["valid_from"], reverse=True)
    return {"entities": entities, "unattributed": unattributed}
