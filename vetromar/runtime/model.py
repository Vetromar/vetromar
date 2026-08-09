"""Model provisioning — pull the qwen weights inside Vetromar, no `ollama pull`.

`ensure_model` streams `ollama.pull` progress to a callback so setup can show a
bar; it's idempotent (already-present is instant). The extraction call in
local_backend.py uses the default client, which resolves the same host, so a
model pulled here is the model it runs.
"""

from __future__ import annotations

from typing import Callable

# progress(label, completed_bytes_or_None, total_bytes_or_None)
Progress = Callable[[str, "int | None", "int | None"], None]


def _client(config):
    import ollama

    return ollama.Client(host=config.ollama_host)


def _model_names(config) -> list[str]:
    listing = _client(config).list()
    names: list[str] = []
    for entry in getattr(listing, "models", listing.get("models", []) if isinstance(listing, dict) else []):
        name = getattr(entry, "model", None) or (entry.get("model") if isinstance(entry, dict) else None)
        if name:
            names.append(name)
    return names


def model_present(config) -> bool:
    """True if config.local_model is already pulled (exact or :latest-normalized)."""
    wanted = config.local_model
    present = _model_names(config)
    if wanted in present:
        return True
    # `qwen3.5:9b` should match a bare `qwen3.5:9b` regardless of a :latest alias.
    base = wanted.split(":", 1)[0]
    return any(name == wanted or name.split(":", 1)[0] == base and ":" not in wanted for name in present)


def model_files_present(config) -> bool:
    """True if the model's weights are on disk in the managed models dir —
    a pure filesystem probe, unlike `model_present` which needs a running
    server. This is the "downloaded?" primitive for Settings/health; Ollama
    writes the manifest last, so its presence implies the blobs landed."""
    name, _, tag = config.local_model.partition(":")
    manifest = (
        config.models_dir
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / name
        / (tag or "latest")
    )
    return manifest.is_file()


def ensure_model(config, progress: Progress | None = None) -> None:
    """Pull config.local_model if absent, forwarding streamed progress."""
    if model_present(config):
        if progress:
            progress(f"Model {config.local_model} ready", None, None)
        return
    for chunk in _client(config).pull(config.local_model, stream=True):
        status = getattr(chunk, "status", None) or (
            chunk.get("status") if isinstance(chunk, dict) else None
        )
        completed = getattr(chunk, "completed", None) or (
            chunk.get("completed") if isinstance(chunk, dict) else None
        )
        total = getattr(chunk, "total", None) or (
            chunk.get("total") if isinstance(chunk, dict) else None
        )
        if progress:
            progress(status or f"Pulling {config.local_model}", completed, total)
