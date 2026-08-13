import json
from pathlib import Path

import pytest

from vetromar.schema import (
    ExtractedUnit,
    GroundedQuote,
    Objection,
    PersonRef,
    RejectedAlternative,
    Status,
    Transcript,
)
from vetromar.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_ambient_workspace_session(monkeypatch, tmp_path):
    """Tests must not pick up a real workspace sign-in from this machine —
    bare Config() loads cloud_token from ~/.vetromar/credentials-cloud, which
    would silently flip managed-tier behavior (AI factory, transcription auto
    mode). Point both session files at non-existent scratch paths; tests that
    exercise sign-in override these envs themselves."""
    monkeypatch.setenv("VETROMAR_CLOUD_CREDENTIALS", str(tmp_path / "no-credentials-cloud"))
    monkeypatch.setenv("VETROMAR_WORKSPACE_CACHE", str(tmp_path / "no-workspace.json"))


@pytest.fixture(autouse=True)
def _isolated_graph_registry(monkeypatch, tmp_path):
    """Tests must never read or write the real ~/.vetromar/graphs.json —
    the multi-graph registry is machine state, same hazard class as the
    workspace session above. Both paths are call-time env-resolved
    (graphs.registry_path / graphs_root) precisely so this works."""
    monkeypatch.setenv("VETROMAR_GRAPHS_REGISTRY", str(tmp_path / "graphs.json"))
    monkeypatch.setenv("VETROMAR_GRAPHS_DIR", str(tmp_path / "graphs"))


@pytest.fixture(autouse=True)
def _no_ambient_signup_notify(monkeypatch):
    """CLOUD_SIGNUP_NOTIFY_EMAIL set in the ambient env would make every
    cloud signup in tests send an operator-notification email, breaking the
    tests that assert nothing was sent. Tests that exercise the
    notification set the var themselves."""
    monkeypatch.delenv("CLOUD_SIGNUP_NOTIFY_EMAIL", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_openai_key(monkeypatch):
    """An ambient OPENAI_API_KEY would silently flow into Config() on dev
    machines that have one set. Tests that exercise the OpenAI-compatible
    provider pass keys explicitly."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_model_caches(monkeypatch, tmp_path):
    """The local-model presence probes (transcription/assets.py) read the real
    HF/torch caches — on a warm dev machine that would make tests pass or fail
    by machine state (the M16 conftest lesson). Point both caches at empty
    scratch dirs; tests that need "downloaded" state build the cache layout
    themselves (see test_local_models.py)."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf-cache"))
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch-home"))


def warm_fake_transcription_caches(
    whisper_repo: str = "Systran/faster-whisper-large-v3",
    diarization_repo: str = "pyannote-community/speaker-diarization-community-1",
) -> None:
    """Fabricate the exact cache files the transcription-model probes look
    for, inside the scratch caches _no_ambient_model_caches points at — the
    'models are downloaded' state without any download."""
    import os

    rev = "0" * 40
    hf = Path(os.environ["HF_HUB_CACHE"])
    for repo_id, files in (
        (whisper_repo, ["model.bin"]),
        (
            diarization_repo,
            ["config.yaml", "segmentation/pytorch_model.bin", "embedding/pytorch_model.bin"],
        ),
    ):
        repo = hf / ("models--" + repo_id.replace("/", "--"))
        (repo / "snapshots" / rev).mkdir(parents=True, exist_ok=True)
        (repo / "refs").mkdir(exist_ok=True)
        (repo / "refs" / "main").write_text(rev)
        for name in files:
            path = repo / "snapshots" / rev / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
    ckpt = Path(os.environ["TORCH_HOME"]) / "hub" / "checkpoints"
    ckpt.mkdir(parents=True, exist_ok=True)
    (ckpt / "wav2vec2_fairseq_base_ls960_asr_ls960.pth").write_text("x")


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch):
    """Tests must never download/load the real embedding model (network + a
    write to ~/.vetromar). Search paths degrade to FTS-only unless a test
    stubs the embed functions explicitly (see test_search.py)."""
    from vetromar.search import embedder

    def unavailable(*args, **kwargs):
        raise embedder.EmbedderUnavailableError("embedder disabled in tests")

    monkeypatch.setattr(embedder, "get_embedder", unavailable)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def billing_transcript() -> Transcript:
    return Transcript.model_validate(
        json.loads((FIXTURES / "billing_rearchitecture.json").read_text())
    )


@pytest.fixture
def reversal_transcript() -> Transcript:
    return Transcript.model_validate(
        json.loads((FIXTURES / "standup_reversal.json").read_text())
    )


def make_billing_unit() -> ExtractedUnit:
    """A hand-built, schema-valid unit whose quotes ARE literal spans of the
    billing fixture — the shape a good extraction should produce."""
    return ExtractedUnit(
        decision="Move billing off the monolith: invoicing service first, kickoff after the data layer migration freeze",
        reasoning="Invoice runs time out (40 min for the EU batch) and billing table locks block checkout writes; pool splits and read replicas were tried/considered and don't fix write contention",
        rejected_alternatives=[
            RejectedAlternative(
                alternative="Give billing its own connection pool",
                why_rejected="Tried in March; bought a month, doesn't fix billing table locks blocking checkout writes",
            ),
            RejectedAlternative(
                alternative="Read replicas for invoice runs",
                why_rejected="Runs write invoice state back; the problem is write contention, not read load",
            ),
        ],
        advocate=PersonRef(ref="SPEAKER_02"),
        objectors=[
            Objection(
                person=PersonRef(ref="SPEAKER_03"),
                grounds="Two migrations at once risks a broken quarter; sequencing matters; wants a rollback plan before kickoff",
            )
        ],
        status=Status.DECIDED,
        grounded_quotes=[
            GroundedQuote(
                text="Decision: billing comes off the monolith, invoicing service first, kickoff after the data layer freeze.",
                speaker=PersonRef(ref="SPEAKER_00"),
                start_ms=70400,
                end_ms=79000,
            )
        ],
    )
