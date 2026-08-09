"""The sync loop — an LLM agent absorbs every source's tool-shape differences.

This is how "no per-source code" survives contact with heterogeneous MCP
servers: Vetromar never knows what `list_messages` or `search_pages` is. The
API model gets the source server's discovered (read-only) tools plus two
internal ones — deliver_episodes and set_cursor — and drives the fetch
itself. Delivered episodes go through deliver_episode (idempotent) and then
the existing generic extraction + auto-linking; the engine only ever sees
(raw text, external_id, evidence). API backend only, like generic extraction.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anyio

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction.validate import ExtractionGateError
from vetromar.providers import ToolSpec
from vetromar.sources.client import open_session
from vetromar.sources.deliver import deliver_episode
from vetromar.sources.registry import SourceConfig
from vetromar.sources.sync_prompt import (
    SYNC_NUDGE_PROMPT,
    SYNC_SYSTEM_PROMPT,
    build_sync_user_prompt,
)
from vetromar.store import Store

logger = logging.getLogger(__name__)

# Tool round-trip budget per run. Tool results are passed back untruncated, so
# context (and cost) grows fast — a run that can't finish inside the budget
# ends `incomplete` with its deliveries stored, and the next run continues
# (idempotent delivery makes the overlap cheap). Unbounded total coverage at
# bounded per-run cost.
MAX_TURNS = 60
# When the model stops without set_cursor, re-prompt it with a completeness
# checklist this many times before giving up. Cheap models' dominant failure
# mode is declaring done after the first page; one nudge recovers most of it.
MAX_NUDGES = 2

# Name heuristic for tools whose annotations don't declare read-onlyness.
_MUTATING_NAME = re.compile(
    r"(post|send|create|delete|update|write|add|remove|set|archive|invite|"
    r"upload|edit|reply|react|move|assign|close|merge)",
    re.IGNORECASE,
)

_DELIVER_TOOL = ToolSpec(
    name="deliver_episodes",
    description="Deliver fetched source content to the knowledge store as "
    "raw episodes. Content must be verbatim.",
    input_schema={
        "type": "object",
        "properties": {
            "episodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "source_kind": {"type": "string"},
                        "external_id": {"type": "string"},
                        "raw": {"type": "string"},
                        "occurred_at": {"type": "string", "description": "ISO 8601, optional"},
                    },
                    "required": ["title", "source_kind", "external_id", "raw"],
                },
            }
        },
        "required": ["episodes"],
    },
)

_CURSOR_TOOL = ToolSpec(
    name="set_cursor",
    description="Record where this sync ended, as a compact JSON string. "
    "Call exactly once, after all episodes are delivered.",
    input_schema={
        "type": "object",
        "properties": {"cursor": {"type": "string"}},
        "required": ["cursor"],
    },
)


@dataclass
class EpisodeDelivery:
    title: str
    source_kind: str
    external_id: str
    raw: str
    occurred_at: datetime | None = None


@dataclass
class SyncReport:
    source: str
    deliveries: list[EpisodeDelivery] = field(default_factory=list)
    created: list[str] = field(default_factory=list)      # external_ids landed
    duplicates: list[str] = field(default_factory=list)   # external_ids deduped
    units: int = 0
    extraction_failures: list[str] = field(default_factory=list)
    cursor: str | None = None
    dry_run: bool = False
    # The agent ended without set_cursor: the sweep didn't finish. Everything
    # delivered IS stored; the cursor stays put — run sync again to continue.
    incomplete: bool = False


def _make_provider(config: Config):
    from vetromar.ai import get_provider

    return get_provider(config)


def _tool_allowed(tool: Any, allowlist: list[str] | None) -> bool:
    if allowlist is not None:
        return tool.name in allowlist
    annotations = getattr(tool, "annotations", None)
    read_only = getattr(annotations, "readOnlyHint", None)
    if read_only is not None:
        return bool(read_only)
    return not _MUTATING_NAME.search(tool.name)


def _result_text(result: Any) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    if getattr(result, "isError", False):
        return json.dumps({"error": "\n".join(parts) or "tool call failed"})
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # One unambiguous JSON payload beats concatenated text blocks.
        return json.dumps(structured)
    return "\n".join(parts) if parts else "(empty result)"


def _parse_occurred_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _run_agent(
    source: SourceConfig, config: Config, cursor: str | None, full: bool = False
) -> tuple[list[EpisodeDelivery], str | None, bool]:
    """Drive the model over the source's tools; return (deliveries, new
    cursor, incomplete).

    incomplete means the run ended without set_cursor — the sweep didn't
    finish. Deliveries still land (delivery is idempotent) and the cursor
    stays put, so a later run continues where this one left off."""
    provider = _make_provider(config)
    deliveries: list[EpisodeDelivery] = []
    new_cursor: str | None = None
    turns = 0
    nudges = 0

    async with open_session(source) as session:
        discovered = (await session.list_tools()).tools
        external = [t for t in discovered if _tool_allowed(t, source.tools)]
        skipped = [t.name for t in discovered if t not in external]
        if skipped:
            logger.info("sync %s: excluded non-read tools: %s", source.name, skipped)
        tools = [
            ToolSpec(name=t.name, description=t.description or "", input_schema=t.inputSchema)
            for t in external
        ] + [_DELIVER_TOOL, _CURSOR_TOOL]
        external_names = {t.name for t in external}

        conversation = provider.start_conversation(
            system=SYNC_SYSTEM_PROMPT,
            user=build_sync_user_prompt(source.name, source.source_kind, cursor, full=full),
            tools=tools,
            max_tokens=16000,
        )
        while turns < MAX_TURNS:
            turns += 1
            try:
                turn = conversation.step()
            except Exception as exc:
                if deliveries:
                    # Mid-crawl fault (context overflow on a huge source, a
                    # transient API error): keep what was delivered and end
                    # incomplete instead of losing the run. With nothing
                    # delivered the error stays loud — auth/config problems
                    # must surface.
                    logger.warning(
                        "sync %s: model call failed mid-crawl after %d delivered "
                        "episode(s) — stopping incomplete",
                        source.name,
                        len(deliveries),
                        exc_info=True,
                    )
                    break
                from vetromar.ai import map_ai_error

                mapped = map_ai_error(exc, config)
                if mapped is not None:
                    raise mapped from exc
                raise
            if not turn.tool_calls:
                if new_cursor is None and nudges < MAX_NUDGES:
                    # Stopped without completing the sweep — re-prompt with the
                    # completeness checklist (the cheap-model early-stop fix).
                    nudges += 1
                    logger.info(
                        "sync %s: agent stopped without set_cursor — nudging (%d/%d)",
                        source.name, nudges, MAX_NUDGES,
                    )
                    conversation.add_user_text(SYNC_NUDGE_PROMPT)
                    continue
                break
            results: list[tuple[str, str]] = []
            for call in turn.tool_calls:
                if call.name == "deliver_episodes":
                    for ep in call.input.get("episodes", []):
                        deliveries.append(
                            EpisodeDelivery(
                                title=ep["title"],
                                source_kind=ep["source_kind"],
                                external_id=ep["external_id"],
                                raw=ep["raw"],
                                occurred_at=_parse_occurred_at(ep.get("occurred_at")),
                            )
                        )
                    output = json.dumps({"delivered": len(call.input.get("episodes", []))})
                elif call.name == "set_cursor":
                    new_cursor = call.input["cursor"]
                    output = json.dumps({"ok": True})
                elif call.name in external_names:
                    logger.info("sync %s: calling %s", source.name, call.name)
                    result = await session.call_tool(call.name, call.input)
                    output = _result_text(result)
                else:
                    output = json.dumps({"error": f"tool {call.name} is not available"})
                results.append((call.id, output))
            if turn.ended and new_cursor is None and nudges < MAX_NUDGES:
                nudges += 1
                logger.info(
                    "sync %s: agent ended turn without set_cursor — nudging (%d/%d)",
                    source.name, nudges, MAX_NUDGES,
                )
                conversation.add_tool_results(results, trailing_text=SYNC_NUDGE_PROMPT)
                continue
            conversation.add_tool_results(results)
            if turn.ended:
                break
    incomplete = new_cursor is None
    logger.info(
        "sync %s: agent finished after %d turn(s) — %d episode(s) delivered%s",
        source.name,
        turns,
        len(deliveries),
        ", INCOMPLETE (no cursor set — run sync again to continue)" if incomplete else "",
    )
    return deliveries, new_cursor, incomplete


def sync_source(
    store: Store,
    source: SourceConfig,
    config: Config,
    *,
    full: bool = False,
    dry_run: bool = False,
    extract: bool = True,
) -> SyncReport:
    """Sync one source: agent fetch -> idempotent delivery -> generic
    extraction (per-episode failures logged, never fatal) -> cursor advance.
    The cursor only advances after delivery succeeded; --dry-run writes
    nothing at all."""
    if config.backend != "api":
        raise ConfigError(
            "Source sync runs on the API backend only.",
            hint="Set VETROMAR_BACKEND=api (local mode still captures meetings).",
        )
    from vetromar.ai import API_KEY_HINT, ai_available

    if not ai_available(config):
        raise ConfigError(
            "No AI provider configured — source sync needs one.",
            hint=API_KEY_HINT,
        )
    state = None if full else store.get_sync_state(source.name)
    cursor = state[0] if state else None

    deliveries, new_cursor, incomplete = anyio.run(_run_agent, source, config, cursor, full)
    report = SyncReport(
        source=source.name, deliveries=deliveries, dry_run=dry_run, incomplete=incomplete
    )

    if dry_run:
        from vetromar.sources.deliver import _content_hash

        for d in deliveries:
            existing = store.get_episode_by_external_id(d.external_id)
            if existing and existing.raw and _content_hash(existing.raw) == _content_hash(d.raw):
                report.duplicates.append(d.external_id)
            else:
                report.created.append(d.external_id)
        report.cursor = new_cursor
        return report

    from vetromar.extraction.generic import extract_from_raw

    for d in deliveries:
        result = deliver_episode(
            store,
            title=d.title,
            source_kind=d.source_kind,
            raw=d.raw,
            external_id=d.external_id,
            occurred_at=d.occurred_at,
        )
        if not result.created:
            report.duplicates.append(d.external_id)
            continue
        report.created.append(result.episode.external_id)
        if extract:
            try:
                units = extract_from_raw(store, result.episode, config)
                report.units += len(units)
            except (ExtractionGateError, RuntimeError) as exc:
                # The raw episode IS landed; only its interpretation failed.
                logger.warning(
                    "sync %s: extraction failed for %s: %s",
                    source.name, result.episode.external_id, exc,
                )
                report.extraction_failures.append(result.episode.external_id)
    if new_cursor is not None:
        store.set_sync_state(source.name, new_cursor)
        report.cursor = new_cursor
    return report
