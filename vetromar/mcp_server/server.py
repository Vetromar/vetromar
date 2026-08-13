"""The MCP surface — the customer's own agent queries AND feeds the store here.

This is the sole integration surface: we build no connectors; the agent brings
the content. Reads carry provenance (episode + method + timestamps) and edges,
because a fact the agent can't trace or fuse is worthless. Writes are the
generic ingestion surface — raw episodes plus evidence-gated units; every
pushed unit passes the same literal-evidence gate as captured ones, and a
batch with any gate failure is rejected whole. No reasoning, no answers
product: the intelligence is rented and lives on the customer's side; we are
the substrate. Permission scoping is a deliberate v0 stub (one team, everyone
sees everything).

Episode `raw` (full transcripts / source text) is excluded from read payloads
unless explicitly requested — it can be huge.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from vetromar import graphs as graph_registry
from vetromar import views
from vetromar.config import load_config
from vetromar.schema import UnitDraft
from vetromar.store import Store

mcp = FastMCP("vetromar")

# One open Store per graph for the process lifetime — the same singleton
# semantics (and FastMCP worker-thread exposure) the pre-multi-graph global
# had; if FastMCP's thread pool ever rotates threads, fall back to a
# per-call Store. `graph=None` on every tool means the private graph, so
# existing agent configs keep working unchanged.
_stores: dict[str, Store] = {}


def _get_store(graph: str | None = None) -> Store:
    graph_id = graph or graph_registry.PRIVATE_GRAPH_ID
    store = _stores.get(graph_id)
    if store is None:
        # open_store attaches the contributor stamp — agent pushes into a
        # shared graph are attributed like any other write.
        store = graph_registry.open_store(graph_id)
        _stores[graph_id] = store
    return store


@mcp.tool()
def list_graphs() -> list[dict]:
    """List the knowledge graphs on this machine: the private graph plus any
    shared graphs the user has created or joined. Pass a graph's `id` as the
    `graph` argument of any other tool to read/write that graph; omitting it
    always means the private graph."""
    return [g.to_dict() for g in graph_registry.list_graphs()]


@mcp.tool()
def search_units(
    text: str | None = None,
    type: str | None = None,
    status: str | None = None,
    episode_id: str | None = None,
    method: str | None = None,
    current_only: bool = False,
    as_of: str | None = None,
    k: int = 10,
    graph: str | None = None,
) -> list[dict]:
    """Search knowledge units. With `text`: hybrid ranked search (keyword +
    semantic — paraphrases match) returning the top `k` after filters. Without
    `text`: a filtered listing. Filters: type (decision/claim/commitment/
    question/metric), status (Decided/Leaning/Parked — decisions only),
    episode_id, method (captured/concierge/pushed/derived), current_only
    (exclude superseded), as_of (ISO datetime — only units that were valid at
    that moment; use it to ask what the company believed back then)."""
    return views.search_units(
        _get_store(graph),
        text,
        type=type,
        status=status,
        episode_id=episode_id,
        method=method,
        current_only=current_only,
        as_of=as_of,
        k=k,
    )


@mcp.tool()
def get_context(
    query: str, token_budget: int = 2000, as_of: str | None = None, graph: str | None = None
) -> dict:
    """One token-budgeted context block answering "what does the store know
    about this": current facts, the people/things involved, what changed or
    was contradicted, and verbatim evidence — every line tagged with its unit
    id for drill-down via get_unit. Prefer this over search_units when you
    want ready-to-use context instead of raw records. Pass as_of (ISO
    datetime) to time-travel the whole block."""
    from vetromar.context import build_context

    return build_context(
        _get_store(graph), query, token_budget=token_budget, as_of=views.parse_as_of(as_of)
    )


@mcp.tool()
def get_unit(unit_id: str, graph: str | None = None) -> dict:
    """Fetch one unit with its provenance (episode) and its edges to other
    units/entities — the fused context, not just the record."""
    store = _get_store(graph)
    return views.unit_payload(store, store.get_unit(unit_id))


@mcp.tool()
def list_episodes(
    limit: int | None = None, offset: int = 0, graph: str | None = None
) -> list[dict]:
    """List episodes (meetings, threads, docs, ...) in the store, oldest
    first. Raw content is omitted; use get_episode(include_raw=True) for it.
    Pass limit/offset to page through a large store."""
    store = _get_store(graph)
    return [views.episode_dict(e) for e in store.list_episodes(limit=limit, offset=offset)]


@mcp.tool()
def get_episode(episode_id: str, include_raw: bool = False, graph: str | None = None) -> dict:
    """Fetch one episode and the units it produced. Set include_raw=True to
    also get the raw content (can be a full transcript — large)."""
    return views.episode_detail(_get_store(graph), episode_id, include_raw=include_raw)


@mcp.tool()
def list_entities(
    type: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    graph: str | None = None,
) -> list[dict]:
    """List entities (people, projects, products, ...) and the ref strings
    (aliases) known to denote each. Optionally filter by entity type; pass
    limit/offset to page through a large store."""
    store = _get_store(graph)
    return [
        json.loads(e.model_dump_json())
        for e in store.list_entities(type=type, limit=limit, offset=offset)
    ]


@mcp.tool()
def units_by_entity(entity_id: str, graph: str | None = None) -> list[dict]:
    """Units that involve this entity — via its mentions/about edges plus its
    linked ref strings (advocate, objector, owner, quoted speaker, author)."""
    store = _get_store(graph)
    return [views.unit_payload(store, u) for u in store.units_by_entity(entity_id)]


@mcp.tool()
def current_state(entity_id: str | None = None, graph: str | None = None) -> dict:
    """The current picture: every unit still valid (not superseded), grouped
    by the entity it involves — 'what is the current direction' in one call.
    Compact unit rows; use get_unit for full detail. Pass entity_id to focus
    on one entity."""
    return views.current_state(_get_store(graph), entity_id)


# -- write surface (the generic ingestion path) -------------------------------


@mcp.tool()
def ingest_episode(
    title: str,
    source_kind: str,
    raw: str | None = None,
    raw_ref: str | None = None,
    occurred_at: str | None = None,
    external_id: str | None = None,
    graph: str | None = None,
) -> dict:
    """WRITE: land a new raw source episode (an email thread, chat log,
    document, metric pull, ...). Pass the source's raw text as `raw` —
    evidence in later add_units calls is validated as literal spans of it.
    source_kind is an open label, e.g. email_thread/chat/document/metric_pull.
    occurred_at: ISO datetime of when the source event happened.
    external_id: the content's stable id at its source (e.g.
    'slack:C123:1721.42') — pass it when pushing synced content so
    re-delivery can be detected; unique across the store."""
    from vetromar.ingest.generic import ingest_episode as _ingest_episode

    store = _get_store(graph)
    episode = _ingest_episode(
        store,
        title=title,
        source_kind=source_kind,
        raw=raw,
        raw_ref=raw_ref,
        occurred_at=views.parse_as_of(occurred_at),
        external_id=external_id,
    )
    return views.episode_dict(episode)


@mcp.tool()
def add_units(
    episode_id: str, units: list[dict], agent: str | None = None, graph: str | None = None
) -> list[dict]:
    """WRITE: push interpreted units into an episode. Each unit is a draft:
    {content, reasoning?, payload: {kind: decision|claim|commitment|question|
    metric, ...kind fields}, evidence: [...]}. Evidence rules: >=1 item per
    unit; use kind 'excerpt' with text copied VERBATIM from the episode's raw
    (literal substring — paraphrase is rejected), or kind 'datapoint'
    {description, value, at} for metrics. The batch is atomic: one gate
    failure rejects ALL units, naming the offender. Set `agent` to identify
    yourself in provenance."""
    from vetromar.ingest.generic import ingest_units

    store = _get_store(graph)
    drafts = [UnitDraft.model_validate(u) for u in units]
    stored = ingest_units(
        store, episode_id, drafts, method="pushed", agent=agent, config=load_config()
    )
    return [views.unit_payload(store, u) for u in stored]


@mcp.tool()
def link_units(from_id: str, to_id: str, kind: str = "related", graph: str | None = None) -> dict:
    """WRITE: link two units (kinds like related/spawned/contradicts) — the
    fusion edge that makes knowledge traversable across sources."""
    store = _get_store(graph)
    edge = store.add_edge(from_id, to_id, kind, method="pushed")
    return json.loads(edge.model_dump_json())


@mcp.tool()
def invalidate_edge(edge_id: str, at: str | None = None, graph: str | None = None) -> dict:
    """WRITE: close validity on an edge — the relation stopped holding at
    `at` (ISO datetime; now if omitted). History is never deleted; the edge
    stays, bounded in time. Units are closed via supersede_unit, not this."""
    store = _get_store(graph)
    edge = store.invalidate_edge(edge_id, at=views.parse_as_of(at))
    return json.loads(edge.model_dump_json())


@mcp.tool()
def supersede_unit(old_id: str, new_id: str, graph: str | None = None) -> dict:
    """WRITE: close validity on old_id as of new_id's valid_from — the
    bi-temporal reversal. History is never deleted; the old unit stays,
    bounded in time, and a supersedes edge records the reversal."""
    store = _get_store(graph)
    closed = store.supersede(old_id, new_id)
    store.add_edge(new_id, old_id, kind="supersedes", method="pushed")
    return views.unit_payload(store, closed)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
