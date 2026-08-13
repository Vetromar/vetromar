"""The graph registry — the app's map of knowledge graphs.

Every user has exactly one PRIVATE graph (the legacy store at
`config.db_path` — never listed in the registry file, synthesized here so
existing installs need zero migration) plus any number of additional graphs.
An additional graph is a full store of its own under
`~/.vetromar/graphs/<id>/store.db`; blob dirs (uploads/, transcripts/,
recordings/, documents/) derive from the store's parent everywhere in the
engine, so each graph gets isolated blobs for free.

A registry entry with `host_url`/`workspace_id` set is a member's replica of
a hosted shared graph (the store binds to the workspace via its own
`replication_state`, engine.py) — those fields are filled by the join flow;
a bare entry is a local-only graph.

The registry itself is `~/.vetromar/graphs.json` — non-secret, flat JSON,
written atomically. `graph_id` strings are the app-wide handle: every route,
job, and MCP tool that touches a store resolves through here.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from vetromar.config import VETROMAR_HOME, Config, load_config

PRIVATE_GRAPH_ID = "private"


def registry_path() -> Path:
    """graphs.json path — env-overridable at call time (the
    cloud_credentials_path idiom) so tests isolate with monkeypatch."""
    return Path(os.environ.get("VETROMAR_GRAPHS_REGISTRY", str(VETROMAR_HOME / "graphs.json")))


def graphs_root() -> Path:
    return Path(os.environ.get("VETROMAR_GRAPHS_DIR", str(VETROMAR_HOME / "graphs")))


class GraphError(Exception):
    """Unknown graph id or unusable registry entry — routes map this to 404."""


@dataclass
class GraphInfo:
    id: str
    name: str
    kind: str  # "private" | "shared"
    # Membership fields — set by the join/host flows for hosted graphs;
    # None for the private graph and for local-only graphs awaiting hosting.
    host_url: Optional[str] = None
    workspace_id: Optional[str] = None
    role: Optional[str] = None
    handle: Optional[str] = None
    display_name: Optional[str] = None
    joined_at: Optional[str] = None
    last_synced_at: Optional[str] = None

    @property
    def synced(self) -> bool:
        """Whether this graph is connected to a host (vs local-only)."""
        return bool(self.host_url and self.workspace_id)

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_registry() -> list[dict]:
    """Read graphs.json. Missing or malformed reads as empty — same tolerance
    as config.toml: a broken registry must not brick the app (the private
    graph never depends on it)."""
    path = registry_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    entries = data.get("graphs", []) if isinstance(data, dict) else []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def _save_registry(entries: list[dict]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "graphs": entries}, indent=2) + "\n")
    os.replace(tmp, path)


def _private_graph() -> GraphInfo:
    return GraphInfo(id=PRIVATE_GRAPH_ID, name="My graph", kind="private")


def list_graphs() -> list[GraphInfo]:
    """All graphs, private first."""
    graphs = [_private_graph()]
    for entry in _load_registry():
        known = {f for f in GraphInfo.__dataclass_fields__}
        graphs.append(GraphInfo(**{k: v for k, v in entry.items() if k in known}))
    return graphs


def get_graph(graph_id: str) -> GraphInfo:
    for info in list_graphs():
        if info.id == graph_id:
            return info
    raise GraphError(f"unknown graph: {graph_id}")


def graph_dir(graph_id: str) -> Path:
    return graphs_root() / graph_id


def resolve_db_path(graph_id: Optional[str], config: Config | None = None) -> Path:
    """graph_id → the store's SQLite path. None/'private' → the legacy
    `config.db_path` (existing installs untouched)."""
    if graph_id is None or graph_id == PRIVATE_GRAPH_ID:
        config = config or load_config()
        return config.db_path
    info = get_graph(graph_id)
    return graph_dir(info.id) / "store.db"


def create_graph(name: str) -> GraphInfo:
    """Register a new (initially local-only) graph and create its store dir.
    Hosting/joining flows fill in host_url/workspace_id later."""
    name = name.strip()
    if not name:
        raise GraphError("graph name must not be empty")
    graph_id = "g_" + uuid.uuid4().hex[:10]
    info = GraphInfo(id=graph_id, name=name, kind="shared", joined_at=_now_iso())
    graph_dir(graph_id).mkdir(parents=True, exist_ok=True)
    _save_registry(_load_registry() + [info.to_dict()])
    return info


def update_graph(graph_id: str, **fields) -> GraphInfo:
    """Merge membership/connection fields into an entry (join/host flows)."""
    if graph_id == PRIVATE_GRAPH_ID:
        raise GraphError("the private graph has no registry entry to update")
    entries = _load_registry()
    for entry in entries:
        if entry.get("id") == graph_id:
            entry.update({k: v for k, v in fields.items() if k != "id"})
            _save_registry(entries)
            return get_graph(graph_id)
    raise GraphError(f"unknown graph: {graph_id}")


def contributor_for(graph_id: Optional[str]):
    """The ContributorRef to stamp on writes into this graph — None for the
    private graph (no audience to attribute to). Generates the identity on
    first use, same as every other identity touchpoint."""
    if graph_id is None or graph_id == PRIVATE_GRAPH_ID:
        return None
    from vetromar.identity import ensure_identity
    from vetromar.schema import ContributorRef

    info = get_graph(graph_id)
    return ContributorRef(
        public_key=ensure_identity().public_key,
        handle=info.handle,
        display_name=info.display_name,
    )


def open_store(graph_id: Optional[str], config: Config | None = None):
    """Open the graph's Store with its contributor attached — THE way any
    write path should open a graph-selected store."""
    from vetromar.store import Store

    db_path = resolve_db_path(graph_id, config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    store.contributor = contributor_for(graph_id)
    return store


def remove_graph(graph_id: str, *, delete_files: bool = False) -> None:
    """Drop a graph from the registry; optionally delete its store dir.
    The private graph can never be removed."""
    if graph_id == PRIVATE_GRAPH_ID:
        raise GraphError("the private graph cannot be removed")
    entries = _load_registry()
    remaining = [e for e in entries if e.get("id") != graph_id]
    if len(remaining) == len(entries):
        raise GraphError(f"unknown graph: {graph_id}")
    _save_registry(remaining)
    if delete_files:
        shutil.rmtree(graph_dir(graph_id), ignore_errors=True)
