"""Chunked generic extraction: verbatim-slice chunking, the unchanged
single-call path for small sources, per-chunk calls with part labels for big
ones, and the deterministic cross-chunk dedup. The evidence gate always
validates against the FULL episode raw — chunking is extraction-side only."""

from datetime import datetime, timezone

from vetromar.config import Config
from vetromar.extraction import generic as generic_mod
from vetromar.extraction.chunking import SINGLE_CALL_LIMIT, chunk_text
from vetromar.extraction.generic import (
    GenericDraft,
    GenericExtractionResult,
    _dedupe_drafts,
    extract_from_raw,
)
from vetromar.ingest.generic import ingest_episode
from vetromar.schema import ClaimPayload, ExcerptEvidence, UnitDraft

WHEN = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
CFG = Config(backend="api", api_key="sk-test")


def _paragraphs(n, sentence="Fact number {i} was recorded in the minutes."):
    return "\n\n".join(sentence.format(i=i) for i in range(n))


# -- chunk_text ---------------------------------------------------------------


def test_small_text_is_one_chunk():
    assert chunk_text("short text") == ["short text"]


def test_chunks_are_verbatim_slices_covering_the_text():
    text = _paragraphs(1200)  # ~55k chars
    chunks = chunk_text(text)
    assert len(chunks) > 1
    # every chunk is a literal slice, in order, first-to-last covering all
    cursor = 0
    for chunk in chunks:
        start = text.index(chunk, max(0, cursor - 2 * len(chunk)))
        cursor = start + len(chunk)
    assert chunks[0] == text[: len(chunks[0])]
    assert text.endswith(chunks[-1])


def test_chunks_respect_size_and_overlap():
    text = _paragraphs(1200)
    chunks = chunk_text(text, size=16_000, overlap=1_000)
    assert all(len(c) <= 16_000 for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        # the head of each next chunk re-appears at the tail of the previous
        assert nxt[:200] in prev


def test_chunk_boundaries_prefer_paragraph_breaks():
    text = _paragraphs(1200)
    for chunk in chunk_text(text)[:-1]:
        assert chunk.endswith("\n\n")


def test_break_heavy_text_has_bounded_chunk_count():
    text = "a\n\n" * 30_000  # 90k chars of nothing but breaks
    chunks = chunk_text(text, size=16_000, overlap=1_000)
    assert len(chunks) <= 2 * len(text) // 16_000 + 2


# -- extract_from_raw paths ---------------------------------------------------


def _capture_calls(monkeypatch, results):
    calls = []

    def fake(config, episode, text, part=None):
        calls.append((text, part))
        return results[len(calls) - 1]

    monkeypatch.setattr(generic_mod, "_call_model", fake)
    return calls


def test_small_raw_keeps_single_call_without_part(store, monkeypatch):
    ep = ingest_episode(
        store, title="Note", source_kind="note",
        raw="The launch moved to September.", occurred_at=WHEN,
    )
    calls = _capture_calls(monkeypatch, [GenericExtractionResult(units=[])])
    extract_from_raw(store, ep, CFG)
    assert calls == [("The launch moved to September.", None)]


def test_large_raw_is_chunked_with_part_labels_and_progress(store, monkeypatch):
    filler = _paragraphs(30, "Background item {i} with no extractable value.")
    key_fact = "The board approved the acquisition of Meridian Labs."
    raw = filler + "\n\n" + ("x" * SINGLE_CALL_LIMIT) + "\n\n" + key_fact
    ep = ingest_episode(
        store, title="Big doc", source_kind="document", raw=raw, occurred_at=WHEN
    )

    unit = GenericDraft(
        content="Meridian Labs acquisition approved",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=key_fact)],
    )
    calls = []

    def fake(config, episode, text, part=None):
        calls.append((text, part))
        # the model only finds the fact in the chunk that contains it
        units = [unit] if key_fact in text else []
        return GenericExtractionResult(units=units)

    monkeypatch.setattr(generic_mod, "_call_model", fake)
    progress = []

    stored = extract_from_raw(
        store, ep, CFG, on_progress=lambda done, total: progress.append((done, total))
    )

    total = len(calls)
    assert total > 1
    assert [part for _, part in calls] == [(i + 1, total) for i in range(total)]
    assert progress == [(i + 1, total) for i in range(total)]
    # the unit from a late chunk passed the gate against the FULL raw
    assert len(stored) == 1
    assert stored[0].evidence[0].text == key_fact


def test_cross_chunk_duplicates_land_once(store, monkeypatch):
    raw = _paragraphs(1200)
    ep = ingest_episode(
        store, title="Big doc", source_kind="document", raw=raw, occurred_at=WHEN
    )
    dup = GenericDraft(
        content="Fact number 3 was recorded",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text="Fact number 3 was recorded in the minutes.")],
    )

    def fake(config, episode, text, part=None):
        return GenericExtractionResult(units=[dup])  # every chunk re-reports it

    monkeypatch.setattr(generic_mod, "_call_model", fake)
    stored = extract_from_raw(store, ep, CFG)
    assert len(stored) == 1


# -- _dedupe_drafts semantics -------------------------------------------------


def _udraft(content, excerpt):
    return UnitDraft(
        content=content, payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=excerpt)],
    )


def test_dedupe_drops_normalized_content_match():
    a = _udraft("Launch  moved to September", "one")
    b = _udraft("launch moved to september", "two")
    assert _dedupe_drafts([a, b]) == [a]


def test_dedupe_drops_contained_evidence_same_kind():
    a = _udraft("Launch moved", "the launch is moving to September because auth slipped")
    b = _udraft("September launch", "moving to September")
    assert _dedupe_drafts([a, b]) == [a]


def test_dedupe_keeps_distinct_units():
    a = _udraft("Launch moved", "moving to September")
    b = _udraft("Jonas owns roadmap", "Jonas will update the roadmap")
    assert _dedupe_drafts([a, b]) == [a, b]
