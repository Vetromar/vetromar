"""Hybrid search: RRF fusion, FTS-only degrade, post-fusion filters, lazy
vector backfill, and sqlite-vec/numpy parity. The embedder is stubbed with
tiny deterministic vectors — no model download in tests."""

import struct

import pytest

from vetromar.ingest.manual import add_draft, add_source_episode
from vetromar.schema import ClaimPayload, ExcerptEvidence, UnitDraft
from vetromar.search import embedder as embedder_mod
from vetromar.search import hybrid
from vetromar.search.embedder import EmbedderUnavailableError


def _pack(*vals) -> bytes:
    return struct.pack(f"<{len(vals)}f", *vals)


# text-keyword -> stub passage vector (unit sphere-ish, 2-dim)
_VECS = {
    "pool": (1.0, 0.0),
    "saturation": (0.95, 0.31),
    "pricing": (0.0, 1.0),
}


def _stub_embed_passages(texts):
    out = []
    for text in texts:
        for key, vec in _VECS.items():
            if key in text.lower():
                out.append(_pack(*vec))
                break
        else:
            out.append(_pack(0.0, 0.0))
    return out


def _stub_embed_query(text):
    return _pack(1.0, 0.0)


@pytest.fixture
def stub_embedder(monkeypatch):
    monkeypatch.setattr(embedder_mod, "embed_passages", _stub_embed_passages)
    monkeypatch.setattr(embedder_mod, "embed_query", _stub_embed_query)


def _claim(store, episode_id, text):
    return add_draft(store, episode_id, UnitDraft(
        content=text, payload=ClaimPayload(), evidence=[ExcerptEvidence(text=text)],
    ))


@pytest.fixture
def seeded(store):
    ep = add_source_episode(store, title="Thread")
    a = _claim(store, ep.id, "Fix the connection pool timeout")          # FTS + vector hit
    b = _claim(store, ep.id, "Database write saturation is the bottleneck")  # vector-only
    c = _claim(store, ep.id, "Redesign the pricing page")                # neither
    return a, b, c


def test_hybrid_rrf_ranking(store, seeded, stub_embedder):
    a, b, c = seeded
    results = hybrid.search(store, "connection pool")
    ids = [r.unit.id for r in results]
    # a hits both channels -> first; b is close in vector space (a paraphrase
    # FTS can't see) -> surfaces second; c trails on a weak vector rank only.
    assert ids[0] == a.id
    assert ids[1] == b.id
    assert results[0].score > results[1].score


def test_search_backfills_missing_vectors_lazily(store, seeded, stub_embedder):
    assert store.list_embeddings() == []  # concierge ingest wrote no vectors
    hybrid.search(store, "anything")
    assert len(store.list_embeddings()) == 3


def test_fts_only_degrade_when_embedder_unavailable(store, seeded, monkeypatch):
    def unavailable(*args, **kwargs):
        raise EmbedderUnavailableError("offline")

    monkeypatch.setattr(embedder_mod, "embed_passages", unavailable)
    monkeypatch.setattr(embedder_mod, "embed_query", unavailable)

    results = hybrid.search(store, "connection pool")
    assert [r.unit.id for r in results] == [seeded[0].id]  # FTS still answers
    assert store.list_embeddings() == []


def test_filters_apply_after_fusion(store, seeded, stub_embedder):
    a, b, c = seeded
    from vetromar.ingest.manual import supersede

    supersede(store, b.id, a.id)
    current = hybrid.search(store, "connection pool", current_only=True)
    assert b.id not in [r.unit.id for r in current]

    assert hybrid.search(store, "connection pool", type="decision") == []
    assert [r.unit.id for r in hybrid.search(store, "connection pool", method="concierge")][0] == a.id


def test_index_units_never_raises(store, seeded, monkeypatch):
    def boom(texts):
        raise RuntimeError("onnx exploded")

    monkeypatch.setattr(embedder_mod, "embed_passages", boom)
    assert hybrid.index_units(store, [seeded[0]]) == 0  # logged, not raised


def test_sqlite_vec_and_numpy_topk_agree(store):
    if not store.vec_available:
        pytest.skip("sqlite-vec extension not loadable in this environment")
    vectors = {
        "unit_aaa": (1.0, 0.0, 0.0),
        "unit_bbb": (0.7, 0.7, 0.0),
        "unit_ccc": (0.0, 1.0, 0.0),
    }
    for unit_id, vec in vectors.items():
        store.put_embedding(unit_id, _pack(*vec))

    query = _pack(1.0, 0.0, 0.0)
    via_sql = store.vector_topk(query, n=3)
    store.vec_available = False
    via_numpy = store.vector_topk(query, n=3)

    assert [uid for uid, _ in via_sql] == [uid for uid, _ in via_numpy] == [
        "unit_aaa", "unit_bbb", "unit_ccc",
    ]
    for (_, d_sql), (_, d_np) in zip(via_sql, via_numpy):
        assert abs(d_sql - d_np) < 1e-5
