"""Deterministic JSON -> markdown renderer. No model in this step.

Markdown is a VIEW, not the product — it exists for the demo and for the
human's eyeballs. The units in the store are the product.
"""

from __future__ import annotations

from vetromar.schema import Episode, Unit


def _stamp(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def _payload_lines(unit: Unit) -> list[str]:
    payload = unit.payload
    lines: list[str] = []
    if payload.kind == "decision":
        if payload.advocate:
            lines.append(f"**Advocate:** {payload.advocate.ref}")
        if payload.objectors:
            lines.append("**Objections:**")
            for obj in payload.objectors:
                lines.append(f"- {obj.person.ref}: {obj.grounds}")
        if payload.rejected_alternatives:
            lines.append("")
            lines.append("**Rejected alternatives:**")
            for alt in payload.rejected_alternatives:
                lines.append(f"- **{alt.alternative}** — {alt.why_rejected}")
    elif payload.kind == "commitment":
        if payload.owner:
            lines.append(f"**Owner:** {payload.owner.ref}")
        if payload.due:
            lines.append(f"**Due:** {payload.due:%Y-%m-%d}")
    elif payload.kind == "question":
        if payload.raised_by:
            lines.append(f"**Raised by:** {payload.raised_by.ref}")
        if payload.resolved:
            lines.append("**Resolved:** yes")
    elif payload.kind == "metric":
        reading = f"{payload.metric} = {payload.value}"
        if payload.unit:
            reading += f" {payload.unit}"
        lines.append(f"**Reading:** {reading}")
        if payload.at:
            lines.append(f"**At:** {payload.at:%Y-%m-%d %H:%M} UTC")
        if payload.source_system:
            lines.append(f"**Source:** {payload.source_system}")
    return lines


def _evidence_lines(unit: Unit) -> list[str]:
    if not unit.evidence:
        return []
    lines = ["", "**Evidence:**"]
    for ev in unit.evidence:
        if ev.kind == "quote":
            lines.append(f"> [{_stamp(ev.start_ms)}] {ev.speaker.ref}: “{ev.text}”")
        elif ev.kind == "excerpt":
            who = f"{ev.author.ref}: " if ev.author else ""
            lines.append(f"> {who}“{ev.text}”")
        else:
            lines.append(f"> {ev.description} = {ev.value} ({ev.at:%Y-%m-%d %H:%M} UTC)")
    return lines


def render_units(episode: Episode, units: list[Unit]) -> str:
    lines = [
        f"# {episode.title}",
        "",
        f"*{episode.source_kind} · {episode.occurred_at:%Y-%m-%d} · {len(units)} unit(s)*",
        "",
    ]
    for i, unit in enumerate(units, 1):
        # Decisions keep their status as the label; other kinds show their kind.
        payload = unit.payload
        label = payload.status.value if payload.kind == "decision" else payload.kind.capitalize()
        lines.append(f"## {i}. [{label}] {unit.content}")
        lines.append("")
        if unit.reasoning:
            lines.append(f"**Why:** {unit.reasoning}")
            lines.append("")
        lines.extend(_payload_lines(unit))
        lines.extend(_evidence_lines(unit))
        if unit.valid_to is not None:
            lines.append("")
            lines.append(f"*Superseded as of {unit.valid_to:%Y-%m-%d %H:%M} UTC.*")
        lines.append("")
    return "\n".join(lines)
