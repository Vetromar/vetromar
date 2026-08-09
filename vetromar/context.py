"""The context block — one token-budgeted, citation-tagged answer to "what
does the store know about X", fused from every retrieval channel.

The Polygres idea applied to Vetromar: an agent shouldn't have to orchestrate
search + entities + edges + history itself. `build_context` runs hybrid
retrieval (FTS + vectors + entity + graph channels), folds in entity cards
and what changed (superseded/contradicted lineage), and renders one markdown
block trimmed to a token budget. Every line carries its unit id in brackets,
so an agent can quote-with-citation or drill into `get_unit` — the answer
stays traceable to gated evidence.

Shared by the MCP tool (`get_context`) and the HTTP API — same block, both
surfaces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from vetromar.schema import Unit
from vetromar.search import hybrid
from vetromar.store import Store

CHARS_PER_TOKEN = 4  # budget heuristic — good enough for trimming prose
DEFAULT_TOKEN_BUDGET = 2000
_SNIPPET_MAX = 240


def build_context(
    store: Store,
    query: str,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    as_of: Optional[datetime] = None,
) -> dict:
    """{"context": markdown, "citations": [unit ids]} for the query,
    trimmed to ~token_budget tokens. `as_of` time-travels the whole block."""
    hits = hybrid.search(
        store, query, k=20, current_only=(as_of is None), as_of=as_of
    )
    units = [scored.unit for scored in hits]
    citations: list[str] = []
    sections: list[tuple[str, list[str]]] = []

    fact_lines = []
    for unit in units:
        line = f"- {unit.content}"
        if unit.reasoning:
            line += f" — {unit.reasoning}"
        fact_lines.append(f"{line} [{unit.id}]")
        citations.append(unit.id)
    if fact_lines:
        title = "Current facts" if as_of is None else f"Facts as of {as_of.isoformat()}"
        sections.append((title, fact_lines))

    entity_lines = _entity_cards(store, units)
    if entity_lines:
        sections.append(("People & things involved", entity_lines))

    changed_lines = _changed_lines(store, units, citations)
    if changed_lines:
        sections.append(("Changed or contradicted", changed_lines))

    evidence_lines = _evidence_lines(units[:5])
    if evidence_lines:
        sections.append(("Evidence excerpts", evidence_lines))

    markdown = _render_within_budget(query, sections, token_budget)
    return {"context": markdown, "citations": citations}


def _entity_cards(store: Store, units: list[Unit]) -> list[str]:
    """One line per entity the result units mention, most-mentioned first."""
    counts: dict[str, int] = {}
    for unit in units:
        for edge in store.edges_for(unit.id):
            if edge.kind not in ("mentions", "about"):
                continue
            other = edge.from_id if edge.to_id == unit.id else edge.to_id
            if other.startswith("ent_"):
                counts[other] = counts.get(other, 0) + 1
    lines = []
    for entity_id, _n in sorted(counts.items(), key=lambda kv: -kv[1])[:8]:
        try:
            entity = store.resolve_entity(entity_id)
        except Exception:  # noqa: BLE001 — a dangling edge never breaks the block
            continue
        line = f"- {entity.name} ({entity.type})"
        if entity.summary:
            line += f": {entity.summary}"
        lines.append(line)
    return lines


def _changed_lines(
    store: Store, units: list[Unit], citations: list[str]
) -> list[str]:
    """Supersede/contradict lineage touching the result units — the 'this
    moved recently' signal an agent needs before trusting a fact."""
    lines: list[str] = []
    seen: set[str] = set()
    for unit in units:
        for edge in store.edges_for(unit.id):
            if edge.kind not in ("supersedes", "contradicts") or edge.id in seen:
                continue
            seen.add(edge.id)
            other_id = edge.from_id if edge.to_id == unit.id else edge.to_id
            if not other_id.startswith("unit_"):
                continue
            try:
                other = store.get_unit(other_id)
            except Exception:  # noqa: BLE001
                continue
            if edge.kind == "supersedes":
                new_id, old_id = edge.from_id, edge.to_id
                newer = unit if unit.id == new_id else other
                older = other if unit.id == new_id else unit
                lines.append(
                    f"- {newer.content!r} superseded {older.content!r}"
                    f" [{newer.id} > {older.id}]"
                )
            else:
                lines.append(
                    f"- {unit.content!r} contradicts {other.content!r}"
                    f" [{unit.id} vs {other.id}]"
                )
            if other_id not in citations:
                citations.append(other_id)
    return lines[:6]


def _evidence_lines(units: list[Unit]) -> list[str]:
    lines = []
    for unit in units:
        for ev in unit.evidence[:1]:
            text = getattr(ev, "text", None)
            if not text:
                continue
            snippet = text if len(text) <= _SNIPPET_MAX else text[: _SNIPPET_MAX - 1] + "…"
            lines.append(f'- "{snippet}" [{unit.id}]')
    return lines


def _render_within_budget(
    query: str, sections: list[tuple[str, list[str]]], token_budget: int
) -> str:
    """Render sections in priority order, dropping lines from the tail of the
    lowest-priority sections first until the block fits the budget."""
    budget_chars = max(token_budget, 100) * CHARS_PER_TOKEN
    header = f"# Context: {query}\n"
    body: list[str] = []
    used = len(header)
    for title, lines in sections:
        section_header = f"\n## {title}\n"
        kept: list[str] = []
        for line in lines:
            cost = len(line) + 1
            if used + len(section_header) + cost > budget_chars:
                break
            kept.append(line)
            used += cost
        if kept:
            used += len(section_header)
            body.append(section_header + "\n".join(kept))
    if not body:
        return header + "\n(no matching knowledge in the store)"
    return header + "".join(body)
