"""Auto-linking: person-mention tiers (SPEAKER-skip, exact, fuzzy,
auto-create), the local cosine tier (related only — never supersede), the
API-mode pair tier with auto-supersede guards, and failure isolation."""

import struct
from datetime import datetime, timezone

import pytest

from tests.conftest import make_billing_unit
from vetromar.config import Config
from vetromar.ingest import ingest_room
from vetromar.ingest.manual import add_draft, add_source_episode, create_entity, link_alias
from vetromar.linking import auto
from vetromar.linking.prompts import PairVerdict, PairVerdicts
from vetromar.schema import ClaimPayload, ExcerptEvidence, PersonRef, UnitDraft
from vetromar.search import embedder as embedder_mod

WHEN = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
LOCAL = Config(backend="local")
API = Config(backend="api", api_key="sk-test")


def _pack(*vals) -> bytes:
    return struct.pack(f"<{len(vals)}f", *vals)


def _claim(store, episode_id, text, author=None):
    return add_draft(store, episode_id, UnitDraft(
        content=text,
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=text, author=PersonRef(ref=author) if author else None)],
    ))


@pytest.fixture
def stub_vectors(monkeypatch):
    """Deterministic passage/query vectors keyed by content keywords."""
    vecs = {
        "billing": (1.0, 0.0),
        "invoicing": (0.9, 0.436),   # cosine vs billing = 0.90
        "lunch": (0.0, 1.0),         # cosine vs billing = 0.0
    }

    def embed_passages(texts):
        out = []
        for text in texts:
            for key, v in vecs.items():
                if key in text.lower():
                    out.append(_pack(*v))
                    break
            else:
                out.append(_pack(0.0, 0.0))
        return out

    monkeypatch.setattr(embedder_mod, "embed_passages", embed_passages)
    monkeypatch.setattr(embedder_mod, "embed_query", lambda text: embed_passages([text])[0])


# -- person-mention tiers -----------------------------------------------------


def test_speaker_labels_are_never_resolved(store):
    episode, units = ingest_room(store, [make_billing_unit()], title="T", occurred_at=WHEN)
    report = auto.auto_link(store, units, LOCAL)
    # every ref in the billing unit is a SPEAKER_NN label -> no entities, no edges
    assert store.list_entities() == []
    assert store.edges_for(units[0].id) == []
    assert report.mentions == 0
    assert report.errors == []


def test_exact_alias_match_links_with_full_confidence(store):
    priya = create_entity(store, "Priya")
    link_alias(store, priya.id, "priya.k")
    ep = add_source_episode(store, title="Thread")
    unit = _claim(store, ep.id, "Ship the billing fix", author="priya.k")

    auto.auto_link(store, [unit], LOCAL)
    edge = store.edges_for(priya.id, kind="mentions")[0]
    assert (edge.from_id, edge.method, edge.confidence, edge.ref) == (unit.id, "auto-exact", 1.0, "priya.k")


def test_fuzzy_alias_match_links_at_lower_confidence(store):
    create_entity(store, "Priya")
    ep = add_source_episode(store, title="Thread")
    unit = _claim(store, ep.id, "Ship the billing fix", author="priya ")

    auto.auto_link(store, [unit], LOCAL)
    edge = store.edges_for(unit.id, kind="mentions")[0]
    assert edge.method == "auto-fuzzy"
    assert edge.confidence == 0.8


def test_unmatched_named_ref_auto_creates_entity(store):
    ep = add_source_episode(store, title="Thread")
    unit = _claim(store, ep.id, "Ship the billing fix", author="Marta")

    report = auto.auto_link(store, [unit], LOCAL)
    assert report.entities_created == 1
    marta = store.resolve_alias("Marta")
    assert marta.type == "person"
    edge = store.edges_for(marta.id)[0]
    assert edge.method == "auto-create"
    assert edge.rationale is not None


# -- local cosine tier --------------------------------------------------------


def test_local_tier_links_related_above_threshold_and_never_supersedes(store, stub_vectors):
    ep = add_source_episode(store, title="Thread")
    prior = _claim(store, ep.id, "The billing rework starts next sprint")
    auto.auto_link(store, [prior], LOCAL)

    new = _claim(store, ep.id, "The invoicing service is the first carve-out")
    report = auto.auto_link(store, [new], LOCAL)

    edges = store.edges_for(new.id, kind="related")
    assert len(edges) == 1
    assert edges[0].method == "auto-embed"
    assert edges[0].confidence == pytest.approx(0.90, abs=0.01)
    assert report.superseded == 0
    assert store.get_unit(prior.id).valid_to is None  # local mode NEVER closes validity


def test_local_tier_below_threshold_makes_no_edge(store, stub_vectors):
    ep = add_source_episode(store, title="Thread")
    prior = _claim(store, ep.id, "The billing rework starts next sprint")
    auto.auto_link(store, [prior], LOCAL)

    new = _claim(store, ep.id, "Order lunch for the offsite")
    auto.auto_link(store, [new], LOCAL)
    assert store.edges_for(new.id, kind="related") == []


# -- API pair tier + auto-supersede guards ------------------------------------


def _api_link(store, monkeypatch, new_units, verdicts):
    monkeypatch.setattr(auto, "_llm_mentions", lambda config, texts: __import__(
        "vetromar.linking.prompts", fromlist=["MentionResult"]).MentionResult(mentions=[]))
    monkeypatch.setattr(auto, "_llm_pairs", lambda config, pairs: verdicts)
    return auto.auto_link(store, new_units, API)


def test_configured_provider_takes_the_llm_tier(store, stub_vectors, monkeypatch):
    """An available AI provider routes linking through the LLM passes — not
    the embed-only local fallback. (A workspace token alone no longer grants
    AI post-pivot.)"""
    managed = Config(backend="api", api_key="sk-ant-x", cloud_token=None)
    ep = add_source_episode(store, title="Thread")
    old = _claim(store, ep.id, "The billing rework starts next sprint")
    auto.auto_link(store, [old], LOCAL)
    new = _claim(store, ep.id, "The billing rework is cancelled")

    called = {}
    monkeypatch.setattr(auto, "_llm_mentions", lambda config, texts: (
        called.setdefault("mentions", True),
        __import__("vetromar.linking.prompts", fromlist=["MentionResult"]).MentionResult(mentions=[]),
    )[1])
    monkeypatch.setattr(
        auto, "_llm_pairs", lambda config, pairs: PairVerdicts(verdicts=[])
    )
    auto.auto_link(store, [new], managed)
    assert called.get("mentions") is True


def test_api_supersede_above_threshold_closes_old_unit(store, stub_vectors, monkeypatch):
    ep = add_source_episode(store, title="Thread")
    old = _claim(store, ep.id, "The billing rework starts next sprint")
    auto.auto_link(store, [old], LOCAL)
    new = _claim(store, ep.id, "The billing rework is cancelled — invoicing carve-out instead")

    verdicts = PairVerdicts(verdicts=[PairVerdict(
        pair_index=0, relation="supersedes", confidence=0.9, rationale="explicit reversal",
    )])
    report = _api_link(store, monkeypatch, [new], verdicts)

    assert report.superseded == 1
    assert store.get_unit(old.id).valid_to is not None
    edge = store.edges_for(old.id, kind="supersedes")[0]
    assert (edge.from_id, edge.method, edge.confidence) == (new.id, "auto-llm", 0.9)


def test_api_supersede_below_threshold_is_edge_only(store, stub_vectors, monkeypatch):
    ep = add_source_episode(store, title="Thread")
    old = _claim(store, ep.id, "The billing rework starts next sprint")
    auto.auto_link(store, [old], LOCAL)
    new = _claim(store, ep.id, "The billing rework might be rethought")

    verdicts = PairVerdicts(verdicts=[PairVerdict(
        pair_index=0, relation="supersedes", confidence=0.7, rationale="maybe a reversal",
    )])
    report = _api_link(store, monkeypatch, [new], verdicts)

    assert report.superseded == 0
    assert store.get_unit(old.id).valid_to is None            # still current
    assert store.edges_for(old.id, kind="supersedes") != []   # but the signal is recorded


def test_api_cross_kind_supersede_blocked(store, stub_vectors, monkeypatch):
    ep = add_source_episode(store, title="Thread")
    old = _claim(store, ep.id, "The billing rework starts next sprint")
    auto.auto_link(store, [old], LOCAL)
    new = add_draft(store, ep.id, UnitDraft(
        content="Billing rework owner is Jonas",
        payload=__import__("vetromar.schema", fromlist=["CommitmentPayload"]).CommitmentPayload(),
        evidence=[ExcerptEvidence(text="Billing rework owner is Jonas")],
    ))

    verdicts = PairVerdicts(verdicts=[PairVerdict(
        pair_index=0, relation="supersedes", confidence=0.95, rationale="wrongly confident",
    )])
    report = _api_link(store, monkeypatch, [new], verdicts)
    assert report.superseded == 0
    assert store.get_unit(old.id).valid_to is None


def test_api_related_verdict_makes_edge_with_rationale(store, stub_vectors, monkeypatch):
    ep = add_source_episode(store, title="Thread")
    old = _claim(store, ep.id, "The billing rework starts next sprint")
    auto.auto_link(store, [old], LOCAL)
    new = _claim(store, ep.id, "The invoicing carve-out is scoped")

    verdicts = PairVerdicts(verdicts=[PairVerdict(
        pair_index=0, relation="related", confidence=0.85, rationale="same billing workstream",
    )])
    _api_link(store, monkeypatch, [new], verdicts)
    edge = store.edges_for(new.id, kind="related")[0]
    assert edge.rationale == "same billing workstream"


def test_api_same_batch_reversal_is_judged(store, stub_vectors, monkeypatch):
    """An in-meeting reversal: the recalled decision and its reversal land in
    ONE batch — the pair tier must judge later-vs-earlier batch units too."""
    ep = add_source_episode(store, title="Standup")
    recalled = _claim(store, ep.id, "Delete all billing feature flags")
    reversal = _claim(store, ep.id, "Keep the billing residency flags; delete the rest")

    verdicts = PairVerdicts(verdicts=[PairVerdict(
        pair_index=0, relation="supersedes", confidence=0.9, rationale="in-meeting reversal",
    )])
    report = _api_link(store, monkeypatch, [recalled, reversal], verdicts)

    assert report.superseded == 1
    assert store.get_unit(recalled.id).valid_to is not None
    assert store.get_unit(reversal.id).valid_to is None


# -- failure isolation --------------------------------------------------------


def test_linking_failure_never_breaks_ingest(store, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("linking exploded")

    monkeypatch.setattr(auto, "_resolve_person_mentions", boom)
    monkeypatch.setattr(auto, "_link_related_embed", boom)

    ep = add_source_episode(store, title="Thread")
    unit = _claim(store, ep.id, "Still lands", author="Marta")
    report = auto.auto_link(store, [unit], LOCAL)

    assert store.get_unit(unit.id).content == "Still lands"  # ingest unharmed
    assert len(report.errors) == 2
    assert store.edges_for(unit.id) == []


def test_units_by_entity_unions_edges_and_refs(store):
    ep = add_source_episode(store, title="Thread")
    by_ref = _claim(store, ep.id, "Ref-only unit", author="priya.k")
    by_edge = _claim(store, ep.id, "Edge-only unit")

    priya = create_entity(store, "Priya")
    link_alias(store, priya.id, "priya.k")
    store.add_edge(by_edge.id, priya.id, kind="mentions", method="auto-exact", confidence=1.0)

    found = {u.id for u in store.units_by_entity(priya.id)}
    assert found == {by_ref.id, by_edge.id}

def test_pair_prompts_are_chunked_and_windowed(store, stub_vectors, monkeypatch):
    """A big batch produces windowed intra-batch pairs (O(batch), not
    O(batch^2)) sent in prompts of at most MAX_PAIRS_PER_CALL each."""
    ep = add_source_episode(store, title="Big sync")
    units = [_claim(store, ep.id, f"Billing observation number {i}") for i in range(30)]

    from vetromar.linking.prompts import MentionResult

    monkeypatch.setattr(auto, "_llm_mentions", lambda config, texts: MentionResult(mentions=[]))
    monkeypatch.setattr(auto, "_candidates", lambda store_, unit, exclude: [])
    calls = []

    def fake_pairs(config, pairs):
        calls.append(len(pairs))
        return PairVerdicts(verdicts=[])

    monkeypatch.setattr(auto, "_llm_pairs", fake_pairs)
    auto.auto_link(store, units, API)

    expected_pairs = sum(min(i, auto.INTRA_BATCH_WINDOW) for i in range(len(units)))
    assert sum(calls) == expected_pairs
    assert len(calls) > 1
    assert all(n <= auto.MAX_PAIRS_PER_CALL for n in calls)
