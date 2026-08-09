"""Prompts + result models for the auto-linking LLM calls (API mode only).

Separate from the frozen meeting prompt and from the generic extraction
prompt — linking judges relations between ALREADY-GATED units; it never
touches raw sources.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# -- non-person entity mentions ----------------------------------------------

MENTION_SYSTEM_PROMPT = """\
You identify NON-PERSON entities mentioned in knowledge units from a company \
knowledge store: projects, products, teams, tools, and organizations.

Rules:
1. Only concrete named things ("the billing service", "PostHog", "the EU \
data-residency project") — never generic nouns ("the roadmap", "the team").
2. `ref` must be the verbatim substring of the unit text that mentions the \
entity; `name` is your canonical name for it.
3. Skip people entirely — they are resolved separately.
4. `unit_index` is the 0-based index of the unit the mention appears in.
5. confidence in [0,1]: how sure you are this is a real, distinct entity.
"""


class Mention(BaseModel):
    name: str = Field(description="Canonical entity name")
    type: str = Field(description="project | product | team | tool | organization")
    ref: str = Field(description="Verbatim mention substring from the unit text")
    unit_index: int = Field(description="0-based index of the unit containing the mention")
    confidence: float = Field(description="Confidence in [0,1]")


class MentionResult(BaseModel):
    mentions: list[Mention]


def build_mention_prompt(unit_texts: list[str]) -> str:
    numbered = "\n".join(f"{i}: {text}" for i, text in enumerate(unit_texts))
    return f"<units>\n{numbered}\n</units>\n\nList the non-person entities mentioned."


# -- pair relation classification --------------------------------------------

PAIR_SYSTEM_PROMPT = """\
You judge the relation between a NEW knowledge unit and an EXISTING unit from \
a company knowledge store. Both are already validated; your job is only the \
relation:

- duplicate — they assert the same thing.
- supersedes — the NEW unit reverses or replaces the EXISTING one, so the \
existing is no longer current (a later decision changing an earlier one).
- contradicts — they cannot both hold, but neither clearly replaces the other.
- related — same topic or thread of work, worth an edge.
- none — no meaningful relation.

Be CONSERVATIVE with `supersedes`: only when the new unit clearly makes the \
existing one obsolete. Closing a unit's validity is consequential; when in \
doubt, say contradicts or related. confidence in [0,1]; rationale is one line.
"""


class PairVerdict(BaseModel):
    pair_index: int = Field(description="0-based index of the judged pair")
    relation: Literal["related", "duplicate", "contradicts", "supersedes", "none"]
    confidence: float = Field(description="Confidence in [0,1]")
    rationale: str = Field(description="One line: why this relation")


class PairVerdicts(BaseModel):
    verdicts: list[PairVerdict]


def build_pair_prompt(pairs: list[tuple[str, str]]) -> str:
    blocks = []
    for i, (new_text, existing_text) in enumerate(pairs):
        blocks.append(f"pair {i}:\n  NEW: {new_text}\n  EXISTING: {existing_text}")
    body = "\n\n".join(blocks)
    return f"<pairs>\n{body}\n</pairs>\n\nJudge each pair's relation."
