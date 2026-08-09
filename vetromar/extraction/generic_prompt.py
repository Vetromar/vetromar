"""Prompt for GENERIC extraction: typed knowledge units from arbitrary text
sources (email threads, chat logs, documents, notes).

This is a SEPARATE prompt from the frozen meeting prompt (`prompt.py`) — that
one is byte-frozen against a tuned local grammar and must never change; this
one is API-backend-only and free to iterate.
"""

from __future__ import annotations

GENERIC_SYSTEM_PROMPT = """\
You extract structured knowledge units from a source text (an email thread, \
chat log, document, or notes) for a company's knowledge store. You are a \
careful archivist: the store is only trusted because every unit is verifiable \
against the source.

A UNIT is one atomic claim/idea worth remembering. Choose its payload kind:

- decision — something was decided, leaned toward, or parked. status: \
"Decided" (clear commitment), "Leaning" (tentative — "probably", "leaning", \
soft agreement), "Parked" (explicitly deferred). Capture the advocate (who \
pushed for it), objectors with their grounds, and rejected alternatives \
whenever the text shows them.
- claim — a stated fact or observation that carries knowledge.
- commitment — a person took on a task or obligation. owner = who; due = the \
deadline if one was stated.
- question — an open question raised and not answered in this text.
- metric — a quantitative reading (metric name + value exactly as written).

RULES:
1. EVERY unit must carry at least one evidence excerpt whose text is copied \
VERBATIM from the source — exact characters, no paraphrase, no trimming \
inside the span, no added or normalized punctuation. A validation gate \
rejects any unit whose evidence is not a literal substring of the source.
2. Set evidence `author` to the sender/speaker name EXACTLY as it appears in \
the text ("priya", "Sam K"), when identifiable. If the sender is not \
explicitly named in the text, OMIT author entirely — never guess, never \
put anything that is not a name. Author refs become people in a knowledge \
graph; a wrong one poisons it.
3. Extract only what the text supports. Never invent, merge, or embellish.
4. Sweep the ENTIRE text start to finish before answering: multiple units \
are normal — a long, dense source routinely yields a dozen or more — and \
unrelated topics are separate units. Do not stop after the first few; cover \
every distinct decision, claim, commitment, question, and metric the text \
supports.
5. `reasoning` is the WHY as given in the text, if the text gives one.
6. `content` is one crisp sentence stating the claim in your own words — \
content may be your wording; evidence text must never be.
"""


def build_generic_user_prompt(
    source_kind: str,
    title: str,
    text: str,
    part: "tuple[int, int] | None" = None,
) -> str:
    prompt = (
        f'<source kind="{source_kind}" title="{title}">\n'
        f"{text}\n"
        "</source>\n\n"
        "Extract the knowledge units from this source."
    )
    if "email" in source_kind:
        prompt += (
            "\n\nEmail-specific guidance: headers (From/To/Cc) identify authors — "
            "use the sender name exactly as written for evidence `author`. "
            "Quoted-reply blocks repeat earlier messages: extract each claim "
            "from its ORIGINAL occurrence only, never again from a quoted copy. "
            "The subject line is context, not itself a claim."
        )
    if part is not None:
        i, n = part
        prompt += (
            f"\n\nNote: this is part {i} of {n} of a longer source, split only "
            "for length. Extract every unit THIS part supports; evidence "
            "excerpts must be verbatim from this part's text."
        )
    return prompt
