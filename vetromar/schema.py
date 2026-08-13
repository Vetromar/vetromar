"""THE foundational schema.

v2: the store is a universal knowledge graph, not a decision log. Two layers:

- **Model-facing extraction schemas** — what an extraction backend produces.
  `ExtractedUnit` is the meeting extractor's output (FROZEN: its JSON schema
  drives both the Anthropic structured output and the Ollama grammar, tuned at
  length — do not touch). `UnitDraft` is the generalized write-surface shape
  for every other path (generic extraction, agent push, concierge JSON).
- **Store node shapes** — `Unit` (interpreted claim), `Episode` (raw layer),
  `Entity` (person/project/product/...), `Edge` (typed, confidence-carrying
  link). Extraction output MAPS into `Unit` (see `ingest/map.py`); a room-
  captured unit and a pushed/hand-entered unit are the SAME record type,
  distinguished only by provenance.

Design constraints (non-negotiable, from the platform handoff + 2026-07-18
engine review):
- Provenance on every unit from day one.
- Bi-temporal: `valid_from`/`valid_to` model when a fact holds in the world;
  `ingested_at` models when we learned it. Reversals close `valid_to` and
  insert a new Unit — history is never mutated.
- HARD INVARIANT: every unit carries >=1 evidence item pointing into its
  episode's raw content. Evidence is polymorphic per source kind (quote /
  excerpt / datapoint); textual evidence must be a LITERAL span of the raw
  (enforced by extraction.validate, at the extraction gate AND the store door).
- `PersonRef` is an UNRESOLVED handle (a diarization speaker label or a name
  string as spoken/written). Resolution happens as `mentions` edges to
  `Entity` rows — automatic where safe, hand-linked otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, computed_field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# People refs — unresolved by design
# ---------------------------------------------------------------------------


class PersonRef(BaseModel):
    """An unresolved handle for a person: a diarization label ("SPEAKER_02")
    or a name exactly as it appeared in the room/source ("Priya").

    Deliberately NOT a resolved identity — resolution lands as `mentions`
    edges to `Entity` rows, never by rewriting the ref.
    """

    ref: str = Field(description="Speaker label or name string, verbatim from the room or source")


# ---------------------------------------------------------------------------
# The meeting extraction unit (model-facing — FROZEN, drives both backend
# grammars; see extraction/local_backend.py `_UNIT_FIELD_ORDER`)
# ---------------------------------------------------------------------------


class Status(str, Enum):
    DECIDED = "Decided"
    LEANING = "Leaning"
    PARKED = "Parked"


class RejectedAlternative(BaseModel):
    alternative: str = Field(description="An option that was considered")
    why_rejected: str = Field(description="Why it lost")


class Objection(BaseModel):
    person: PersonRef = Field(description="Who pushed back")
    grounds: str = Field(description="On what grounds they objected")


class GroundedQuote(BaseModel):
    """A verbatim excerpt pulled directly from the transcript. NEVER
    paraphrased or regenerated — this is the evidence that makes the unit
    trustable, validated as a literal substring after extraction."""

    text: str = Field(
        description=(
            "Verbatim spoken words copied exactly from the transcript — no timestamp, "
            "no speaker-label prefix, no paraphrase"
        )
    )
    speaker: PersonRef = Field(description="Who said it")
    start_ms: int = Field(description="Start of the span in the recording, milliseconds")
    end_ms: int = Field(description="End of the span in the recording, milliseconds")


class ExtractedUnit(BaseModel):
    """One decision, with its reasoning fused in. The meeting extractor's
    atomic output — maps into a decision-typed `Unit` via ingest/map.py."""

    decision: str = Field(description="What was decided (or leaned toward, or parked)")
    reasoning: str = Field(
        description="WHY — the reasoning fused to the decision, as voiced in the room"
    )
    rejected_alternatives: list[RejectedAlternative] = Field(
        default_factory=list, description="What was considered and why it lost"
    )
    advocate: Optional[PersonRef] = Field(
        default=None, description="Who pushed for this decision, if identifiable"
    )
    objectors: list[Objection] = Field(
        default_factory=list, description="Who pushed back, and on what grounds"
    )
    status: Status = Field(description="Decided, Leaning, or Parked")
    grounded_quotes: list[GroundedQuote] = Field(
        default_factory=list,
        description="Verbatim transcript spans with timestamps evidencing this unit",
    )


class ExtractionResult(BaseModel):
    """Top-level shape both meeting extraction backends must emit."""

    units: list[ExtractedUnit]


# ---------------------------------------------------------------------------
# Typed payloads — the per-kind half of the universal Unit
# ---------------------------------------------------------------------------


class DecisionPayload(BaseModel):
    """A decision with its deliberation context (the original moat fields)."""

    kind: Literal["decision"] = "decision"
    status: Status = Field(description="Decided, Leaning, or Parked")
    advocate: Optional[PersonRef] = Field(
        default=None, description="Who pushed for this decision, if identifiable"
    )
    objectors: list[Objection] = Field(
        default_factory=list, description="Who pushed back, and on what grounds"
    )
    rejected_alternatives: list[RejectedAlternative] = Field(
        default_factory=list, description="What was considered and why it lost"
    )


class ClaimPayload(BaseModel):
    """A stated fact, idea, or observation — the generic knowledge atom."""

    kind: Literal["claim"] = "claim"


class CommitmentPayload(BaseModel):
    """Someone committed to doing something."""

    kind: Literal["commitment"] = "commitment"
    owner: Optional[PersonRef] = Field(default=None, description="Who committed to it")
    due: Optional[datetime] = Field(default=None, description="Deadline, if one was stated")


class QuestionPayload(BaseModel):
    """An open question that was raised and not answered in the source."""

    kind: Literal["question"] = "question"
    raised_by: Optional[PersonRef] = Field(default=None, description="Who raised it")
    resolved: bool = Field(default=False, description="Whether it has since been answered")


class MetricPayload(BaseModel):
    """A quantitative observation pulled from an analytics/source system."""

    kind: Literal["metric"] = "metric"
    metric: str = Field(description="Name of the measured quantity, e.g. 'signup conversion'")
    value: str = Field(description="The reading, stringified exactly as sourced")
    unit: Optional[str] = Field(default=None, description="Unit of measure, e.g. '%', 'ms'")
    at: Optional[datetime] = Field(default=None, description="When the reading was taken")
    source_system: Optional[str] = Field(
        default=None, description="Where it came from, e.g. 'posthog'"
    )


UnitPayload = Annotated[
    Union[DecisionPayload, ClaimPayload, CommitmentPayload, QuestionPayload, MetricPayload],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Evidence — polymorphic per source kind; the trust mechanism
# ---------------------------------------------------------------------------


class QuoteEvidence(GroundedQuote):
    """A literal transcript span with speaker + timestamps (meeting sources)."""

    kind: Literal["quote"] = "quote"


class ExcerptEvidence(BaseModel):
    """A verbatim excerpt from a textual source (email, chat, doc). Must be a
    literal span of the episode's raw content — paraphrase is a correctness bug."""

    kind: Literal["excerpt"] = "excerpt"
    text: str = Field(description="Verbatim span copied exactly from the source text")
    author: Optional[PersonRef] = Field(default=None, description="Who wrote it, if known")
    locator: Optional[str] = Field(
        default=None, description="Pointer within the source, e.g. a message id or heading"
    )


class DataPointEvidence(BaseModel):
    """The datapoint backing a metric observation."""

    kind: Literal["datapoint"] = "datapoint"
    description: str = Field(description="What was measured / the query that produced it")
    value: str = Field(description="The sourced value, stringified")
    at: datetime = Field(description="When the reading was taken")
    locator: Optional[str] = Field(
        default=None, description="Pointer into the episode's raw payload, if applicable"
    )


Evidence = Annotated[
    Union[QuoteEvidence, ExcerptEvidence, DataPointEvidence],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Provenance, Episode (the raw layer), and the universal store Unit
# ---------------------------------------------------------------------------


class ContributorRef(BaseModel):
    """Who put this into a shared graph: their identity's public key plus the
    name they go by in that graph. None on private-graph content — the
    private graph has no audience to attribute to."""

    public_key: str = Field(description="The contributor's Ed25519 public key (urlsafe b64)")
    handle: Optional[str] = Field(default=None, description="Their handle in this graph")
    display_name: Optional[str] = Field(default=None, description="Their display name there")


class Provenance(BaseModel):
    """How a unit entered the store. `method` values:
    'captured' (our pipeline), 'concierge' (hand-entered), 'pushed' (an agent
    wrote it via MCP/API), 'derived' (the engine created it, e.g. generic
    extraction over pushed raw)."""

    method: str = Field(description="captured | concierge | pushed | derived")
    episode_id: str = Field(description="The Episode this came from")
    captured_at: datetime = Field(default_factory=_now)
    agent: Optional[str] = Field(
        default=None, description="Client/agent name for pushed units, if identified"
    )
    contributor: Optional[ContributorRef] = Field(
        default=None,
        description="Who contributed this to a shared graph; None on private content",
    )


class Episode(BaseModel):
    """The raw layer: the event a unit came from, WITH its raw content.
    `source_kind` is what the source is ('meeting', 'email_thread', 'chat',
    'document', 'metric_pull', 'note', ... — open set); entry method lives on
    each unit's Provenance. `raw` is the canonical raw content (transcript
    JSON for meetings, source text otherwise); `raw_ref` points at big
    binaries (audio) or the external source."""

    id: str = Field(default_factory=lambda: _new_id("ep"))
    source_kind: str
    title: str
    occurred_at: datetime
    ingested_at: datetime = Field(default_factory=_now)
    raw: Optional[str] = Field(
        default=None, description="Canonical raw content — evidence is validated against this"
    )
    raw_ref: Optional[str] = Field(
        default=None, description="Pointer to raw material on disk / at the source"
    )
    external_id: Optional[str] = Field(
        default=None,
        description="Stable id at the source ('slack:C123:1721...') for sync"
        " idempotency; None for captured/hand-entered episodes",
    )
    contributor: Optional[ContributorRef] = Field(
        default=None,
        description="Who contributed this to a shared graph; None on private content",
    )


class Unit(BaseModel):
    """The platform store's node shape: one interpreted claim/idea with typed
    payload, evidence, provenance, and bi-temporal validity."""

    id: str = Field(default_factory=lambda: _new_id("unit"))
    content: str = Field(description="The claim/idea — what this unit asserts")
    reasoning: Optional[str] = Field(
        default=None, description="WHY the claim holds, as voiced/written in the source"
    )
    payload: UnitPayload
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="Verifiable evidence pointing into the episode's raw content (>=1 required)",
    )
    provenance: Provenance
    valid_from: datetime = Field(default_factory=_now)
    valid_to: Optional[datetime] = Field(
        default=None, description="Set when this unit is superseded (reversed/re-parked)"
    )
    ingested_at: datetime = Field(default_factory=_now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def type(self) -> str:
        """The unit's kind — mirrors payload.kind (denormalized into the store)."""
        return self.payload.kind


class UnitDraft(BaseModel):
    """The generalized write-surface shape: a Unit before the store assigns
    identity, provenance, and timestamps. What generic extraction emits and
    what agents push over MCP."""

    content: str = Field(description="The claim/idea — what this unit asserts")
    reasoning: Optional[str] = Field(
        default=None, description="WHY the claim holds, as voiced/written in the source"
    )
    payload: UnitPayload
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="Verbatim evidence from the episode's raw content (>=1 required)",
    )


# ---------------------------------------------------------------------------
# Entities and edges — the graph half
# ---------------------------------------------------------------------------


class Entity(BaseModel):
    """A resolved identity: a person, project, product, team... `aliases`
    holds every ref string known to denote this entity (speaker labels,
    handles, name variants) — populated by hand or by auto-resolution."""

    id: str = Field(default_factory=lambda: _new_id("ent"))
    type: str = Field(default="person", description="person | project | product | team | ...")
    name: str = Field(description="Canonical display name")
    aliases: list[str] = Field(
        default_factory=list,
        description="All ref strings known to refer to this entity",
    )
    created_at: datetime = Field(default_factory=_now)
    summary: Optional[str] = Field(
        default=None, description="Short evolving description of who/what this is"
    )
    attributes: dict = Field(
        default_factory=dict,
        description="Open key-value attributes (email, handle, role, url, ...)",
    )
    merged_into: Optional[str] = Field(
        default=None,
        description="Set when deduplicated into another entity — reads follow"
        " the redirect; edges are never rewritten",
    )


class Edge(BaseModel):
    """A typed link between two nodes (units and/or entities). Machine-made
    edges carry method + confidence + rationale so trust stays inspectable;
    human-made edges have confidence=None."""

    id: str = Field(default_factory=lambda: _new_id("edge"))
    from_id: str
    to_id: str
    kind: str = Field(
        default="related",
        description="e.g. 'related', 'contradicts', 'supersedes', 'spawned', 'mentions', 'about'",
    )
    method: str = Field(
        default="manual",
        description="manual | auto-exact | auto-fuzzy | auto-embed | auto-llm",
    )
    confidence: Optional[float] = Field(
        default=None, description="Machine confidence in [0,1]; None for human-made edges"
    )
    rationale: Optional[str] = Field(
        default=None, description="Why the machine made this edge (one line)"
    )
    ref: Optional[str] = Field(
        default=None, description="Verbatim mention string, for mentions/about edges"
    )
    created_at: datetime = Field(default_factory=_now)
    valid_from: Optional[datetime] = Field(
        default=None,
        description="When the linked relation began holding; None reads as created_at",
    )
    valid_to: Optional[datetime] = Field(
        default=None,
        description="Set when the relation stopped holding (invalidated, or an"
        " endpoint unit was superseded); None while current",
    )


# ---------------------------------------------------------------------------
# Transcript (pipeline-internal, upstream of extraction)
# ---------------------------------------------------------------------------


class TranscriptSegment(BaseModel):
    speaker: str  # diarization label, e.g. "SPEAKER_00"
    text: str
    start_ms: int
    end_ms: int


class Transcript(BaseModel):
    segments: list[TranscriptSegment]

    def to_prompt_text(self) -> str:
        """Deterministic rendering handed to the extraction model."""
        lines = []
        for seg in self.segments:
            total_s = seg.start_ms // 1000
            stamp = f"{total_s // 60:02d}:{total_s % 60:02d}"
            lines.append(f"[{stamp}] [{seg.start_ms}-{seg.end_ms}ms] {seg.speaker}: {seg.text}")
        return "\n".join(lines)

    def full_text(self) -> str:
        """Concatenated spoken text, used for literal-quote validation."""
        return "\n".join(seg.text for seg in self.segments)
