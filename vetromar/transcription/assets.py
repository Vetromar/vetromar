"""Local transcription model assets — presence probes and explicit downloads.

Local capture needs three weight sets that used to download silently on first
use: the faster-whisper model (HF hub cache), the English alignment model
(torchaudio pipeline → torch hub cache), and the pyannote diarization repo
(HF hub cache). This module makes them an explicit, user-triggered download
(Settings → Download local models) and gives health/Settings a cheap
filesystem-only probe — no torch/whisperx import on the probe path, so a
torch-free install can still report status (`huggingface_hub` is a base dep).

Both sides use the SAME caches the capture path reads: `download_*` calls the
very loaders capture uses, and the probes look for the files those loaders
leave behind. Partial downloads never probe as present by construction — the
HF hub stages `.incomplete` blobs and torch.hub downloads to a temp file, so
the final filenames only exist when complete.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

# Same 3-arg shape as runtime/model.py and operations.Progress.
Progress = Callable[[str, "int | None", "int | None"], None]

# whisperx's English alignment model is torchaudio's WAV2VEC2_ASR_BASE_960H
# pipeline (whisperx.alignment.DEFAULT_ALIGN_MODELS_TORCH["en"]), which lands
# in the torch hub cache under exactly this filename.
_ALIGN_EN_CHECKPOINT = "wav2vec2_fairseq_base_ls960_asr_ls960.pth"

_DEFAULT_DIARIZATION_MODEL = "pyannote-community/speaker-diarization-community-1"


def _hf_cache_dir() -> Path:
    """The HF hub cache, resolved from env at call time (huggingface_hub
    freezes its constants at import — call-time resolution keeps the probe
    honest under HF_HOME/HF_HUB_CACHE overrides, incl. tests)."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _torch_hub_checkpoints() -> Path:
    if os.environ.get("TORCH_HOME"):
        base = Path(os.environ["TORCH_HOME"])
    else:
        base = Path.home() / ".cache" / "torch"
    return base / "hub" / "checkpoints"


def whisper_repo_id(model: str) -> str:
    """The HF repo faster-whisper resolves a model size to. Uses its own table
    when the capture extra is installed; the Systran naming convention holds
    for every size we'd configure, so the fallback matches."""
    try:
        from faster_whisper.utils import _MODELS

        if model in _MODELS:
            return _MODELS[model]
    except ImportError:
        pass
    return f"Systran/faster-whisper-{model}"


def _hf_file_cached(repo_id: str, filename: str) -> bool:
    from huggingface_hub import try_to_load_from_cache

    found = try_to_load_from_cache(repo_id, filename, cache_dir=str(_hf_cache_dir()))
    return isinstance(found, (str, Path))


def transcription_models_status(config) -> dict:
    """Filesystem-only presence probe for the local transcription weights.

    {"present": all-present, "components": {whisper, align, diarization}} —
    each component {"present": bool, "detail": str}."""
    whisper_repo = whisper_repo_id(config.whisper_model)
    whisper_ok = _hf_file_cached(whisper_repo, "model.bin")

    align_path = _torch_hub_checkpoints() / _ALIGN_EN_CHECKPOINT
    align_ok = align_path.is_file()

    dia_repo = config.diarization_model
    if dia_repo == _DEFAULT_DIARIZATION_MODEL:
        # The community-1 repo is self-contained; require the actual weights,
        # not just the config, so a metadata-only fetch doesn't probe present.
        dia_ok = all(
            _hf_file_cached(dia_repo, f)
            for f in (
                "config.yaml",
                "segmentation/pytorch_model.bin",
                "embedding/pytorch_model.bin",
            )
        )
    else:
        # Dev-only override: unknown layout, best-effort config-level check.
        dia_ok = _hf_file_cached(dia_repo, "config.yaml")

    return {
        "present": whisper_ok and align_ok and dia_ok,
        "components": {
            "whisper": {"present": whisper_ok, "detail": whisper_repo},
            "align": {"present": align_ok, "detail": _ALIGN_EN_CHECKPOINT},
            "diarization": {"present": dia_ok, "detail": dia_repo},
        },
    }


def missing_component_names(status: dict) -> list[str]:
    labels = {
        "whisper": "speech model",
        "align": "alignment model",
        "diarization": "speaker model",
    }
    return [
        labels[name]
        for name, comp in status["components"].items()
        if not comp["present"]
    ]


def download_transcription_models(config, progress: Progress | None = None) -> None:
    """Fetch all three weight sets into the caches capture reads, via the same
    loaders capture uses. Idempotent (each loader is cache-first). Without the
    capture extra there's nothing local capture could run anyway — report the
    skip and return rather than fail the whole download job."""

    def note(label: str) -> None:
        if progress:
            progress(label, None, None)

    try:
        import faster_whisper.utils
        import whisperx
    except ImportError:
        note("Transcription models skipped — audio capture extra not installed")
        return

    status = transcription_models_status(config)
    comps = status["components"]

    if comps["whisper"]["present"]:
        note(f"Speech model ready ({config.whisper_model})")
    else:
        note(f"Downloading speech model ({config.whisper_model}, ~3 GB)")
        # The exact function whisperx.load_model uses — same repo, same
        # allow_patterns, same cache.
        faster_whisper.utils.download_model(config.whisper_model)

    if comps["align"]["present"]:
        note("Alignment model ready")
    else:
        note("Downloading alignment model (~360 MB)")
        # Loading the torchaudio bundle is the download; the loaded model is
        # dropped (brief RAM blip inside the job thread, nothing persists).
        whisperx.load_align_model("en", "cpu")

    if comps["diarization"]["present"]:
        note("Speaker model ready")
    else:
        note("Downloading speaker model (~1 GB)")
        from huggingface_hub import snapshot_download

        # The pinned community-1 repo is self-contained (segmentation/
        # embedding/plda in-repo) — one snapshot is exactly what capture loads.
        snapshot_download(config.diarization_model)
