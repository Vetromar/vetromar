"""Backend operations shared by the CLI and the desktop UI API.

The `vetromar setup` / `capture` / `record` flows and the HTTP API must behave
identically — same provisioning, same host-pinning, same key validation. That
shared behavior lives here as plain functions with no I/O framework (no `typer`,
no `fastapi`): they mutate config/runtime state and raise `ConfigError` (or
`InvalidApiKey`) on failure, and each surface renders the result its own way.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from vetromar.config import (
    VETROMAR_HOME,
    Config,
    save_api_key,
    save_config,
    save_deepgram_api_key,
)
from vetromar.errors import ConfigError

Progress = Callable[[str, "int | None", "int | None"], None]


def default_meeting_title(when: datetime) -> str:
    """The auto-generated title when a capture/record starts without one.
    Rendered in the user's local wall-clock time — the sidecar and CLI run on
    the user's own machine, so the system timezone IS the meeting's timezone."""
    return f"Meeting {when.astimezone():%Y-%m-%d %H:%M}"


# --- MCP access for the customer's own agent ---------------------------------
#
# `vetromar serve` (the stdio MCP server) only exists on PATH for dev installs;
# on a customer machine the CLI lives buried inside the .app's sidecar bundle.
# The shim gives coding agents ONE stable path that survives app moves and
# updates: ~/.vetromar/bin/vetromar-mcp, rewritten at every ui_server boot to
# exec whatever engine is currently running (frozen sidecar or dev CLI).


def mcp_shim_path() -> Path:
    return VETROMAR_HOME / "bin" / "vetromar-mcp"


def _engine_command() -> str | None:
    """Absolute path of the command that runs THIS engine's CLI. In the frozen
    bundle the sidecar binary IS the CLI (sidecar_entry is surface-identical);
    in dev it's the console script next to the interpreter, else PATH."""
    if getattr(sys, "frozen", False):
        return sys.executable
    candidate = Path(sys.executable).parent / "vetromar"
    if candidate.exists():
        return str(candidate)
    return shutil.which("vetromar")


def install_mcp_shim() -> Path | None:
    """Write the shim (POSIX sh — the desktop app ships macOS-first). Returns
    None when the engine command can't be resolved (exotic dev runs), which
    callers treat as 'CLI users already know `vetromar serve`'."""
    command = _engine_command()
    if command is None:
        return None
    path = mcp_shim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "# Vetromar MCP entry point — rewritten by the Vetromar app on every\n"
        "# launch; do not edit. Runs the local knowledge-base MCP server (stdio).\n"
        f'exec {shlex.quote(command)} serve "$@"\n'
    )
    path.chmod(0o755)
    return path


def mcp_access_info() -> dict:
    """What the UI surfaces: the shim path plus the paste-into-your-agent
    prompt (one source of truth for that copy — the frontend never composes it)."""
    shim = mcp_shim_path()
    prompt = (
        "Connect to my Vetromar knowledge base over MCP so you can query it. "
        f'It is a local stdio MCP server: the command is "{shim}" with no '
        "arguments. Register it in your MCP configuration under the name "
        '"vetromar", then use its tools (search_units, current_state, '
        "list_episodes, ...) to answer questions from my team's decisions, "
        "meetings, and synced sources."
    )
    return {"shim_path": str(shim), "installed": shim.exists(), "prompt": prompt}


class InvalidApiKey(Exception):
    """A key was rejected by a live auth check. Not a ConfigError: the user
    supplied a value, it's just wrong — surface it inline, not as a
    'run setup' hint."""


def ensure_backend_ready(config: Config) -> None:
    """In local mode, guarantee the managed Ollama server is answering before
    extraction, and pin the frozen `ollama.chat` default client to OUR server.

    `setup` provisions the binary + model, but the server it spawned may have
    exited; the extraction call never starts one itself. This reuses a running
    server and only spawns the managed binary if needed — provisioning, not the
    frozen extraction call path. No-op in api mode."""
    if config.backend != "local":
        return
    from vetromar import runtime

    runtime.ensure_server(config)
    # The frozen `ollama.chat` uses the default client, which reads OLLAMA_HOST.
    # Pin it to our managed server (dedicated port) so extraction never lands on
    # a system Ollama the user may be running. The call itself is untouched.
    os.environ["OLLAMA_HOST"] = config.ollama_host


def validate_and_save_api_key(key: str) -> None:
    """Validate an Anthropic key with a live call, then persist it (0600
    credentials) and select the api backend + anthropic provider. Raises
    InvalidApiKey if rejected."""
    import anthropic

    key = key.strip()
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError as exc:
        raise InvalidApiKey("That key was rejected by Anthropic.") from exc
    save_api_key(key)
    save_config({"backend": "api", "ai_provider": "anthropic"})
    prefetch_embedding_model()


def configure_provider(
    provider: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> None:
    """Validate + persist a BYO AI provider (the Settings surface).

    anthropic: live-checks the key, persists it, selects the provider.
    openai: live-checks the endpoint with the given values BEFORE anything is
    persisted (a rejected key must never land in the credentials file).
    Raises InvalidApiKey on a rejected key, ConfigError otherwise."""
    if provider == "anthropic":
        if not (api_key or "").strip():
            raise ConfigError(
                "An Anthropic API key is required.",
                hint="Create one at console.anthropic.com and paste it here.",
            )
        validate_and_save_api_key(api_key)
        if model and model.strip():
            save_config({"api_model": model.strip()})
        return
    if provider != "openai":
        raise ConfigError(f"Unknown AI provider {provider!r}.")

    from types import SimpleNamespace

    from vetromar.providers import CredentialsRejected
    from vetromar.providers.openai_compat import OpenAICompatProvider

    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ConfigError(
            "A base URL is required for an OpenAI-compatible provider.",
            hint="e.g. https://api.openai.com/v1, or http://localhost:11434/v1 for Ollama.",
        )
    chosen_model = (model or "").strip()
    if not chosen_model:
        raise ConfigError(
            "A model name is required.",
            hint="Use a model this endpoint serves (e.g. gpt-5-mini, or an Ollama tag).",
        )
    key = (api_key or "").strip() or None
    probe_config = SimpleNamespace(
        openai_base_url=url, openai_api_key=key, api_model=chosen_model
    )
    try:
        OpenAICompatProvider(probe_config).check_credentials()
    except CredentialsRejected as exc:
        raise InvalidApiKey(str(exc)) from exc
    if key:
        from vetromar.config import save_openai_api_key

        save_openai_api_key(key)
    save_config(
        {
            "backend": "api",
            "ai_provider": "openai",
            "openai_base_url": url,
            "api_model": chosen_model,
        }
    )
    prefetch_embedding_model()


def provider_settings(config: Config) -> dict:
    """Current provider configuration for the Settings UI — presence flags
    only for secrets, never the secrets themselves."""
    return {
        "provider": config.ai_provider,
        "model": config.api_model,
        "openai_base_url": config.openai_base_url,
        "has_anthropic_key": bool(config.api_key),
        "has_openai_key": bool(config.openai_api_key),
        "has_deepgram_key": bool(config.deepgram_api_key),
    }


def select_api_backend(config: Config) -> None:
    """Select the cloud (api) backend. Requires a configured AI provider.
    Also restores transcription to "auto" (the fast tier resolves by
    Deepgram-key presence)."""
    from vetromar.ai import API_KEY_HINT, ai_available

    if not ai_available(config):
        raise ConfigError(
            "Configure an AI provider to use cloud mode.",
            hint=API_KEY_HINT,
        )
    save_config({"backend": "api", "transcribe": "auto"})
    prefetch_embedding_model()


def select_local_backend(config: Config) -> None:
    """Switch to fully-local mode — extraction AND transcription on this
    machine (audio/text never leave it). Persists the choice only; model
    downloads are a separate explicit step (`download_local_models`)."""
    save_config(
        {
            "backend": "local",
            "transcribe": "local",
            "local_model": config.local_model,
        }
    )
    prefetch_embedding_model()


def _check_deepgram_key(key: str) -> bool:
    """Live auth check: GET the key's own token details — the cheapest
    authenticated Deepgram call (no audio, no billing). True iff accepted."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://api.deepgram.com/v1/auth/token",
        headers={"Authorization": f"Token {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False
        raise ConfigError(
            f"Deepgram auth check failed (HTTP {exc.code}).",
            hint="Try again in a moment, or check https://status.deepgram.com.",
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConfigError(
            f"Could not reach Deepgram to validate the key ({exc}).",
            hint="Check your connection and try again.",
        ) from exc


def validate_and_save_deepgram_key(key: str) -> None:
    """Validate a Deepgram key with a live call, then persist it (0600
    per-provider credentials). Raises InvalidApiKey if rejected. No config.toml
    write: the default 'auto' transcription mode flips to cloud by key
    presence alone."""
    key = key.strip()
    if not _check_deepgram_key(key):
        raise InvalidApiKey("That key was rejected by Deepgram.")
    save_deepgram_api_key(key)


def download_local_models(config: Config, progress: Progress | None = None) -> None:
    """The one explicit local-model download: Ollama runtime + extraction
    model (~6.6 GB), then the transcription weights (~2 GB), then the
    embedding model. Idempotent; never changes the selected backend."""
    from vetromar import runtime
    from vetromar.transcription import assets

    runtime.ensure_local_ready(config, progress=progress)
    assets.download_transcription_models(config, progress=progress)
    prefetch_embedding_model(progress)


def local_models_status(config: Config) -> dict:
    """Filesystem-only "downloaded?" snapshot per component — the data behind
    the Settings local-models section. Never touches the network or a server."""
    from vetromar import runtime, search
    from vetromar.transcription import assets

    return {
        "extraction": {
            "model": config.local_model,
            "runtime": runtime.resolve_binary(config) is not None,
            "model_present": runtime.model_files_present(config),
        },
        "transcription": assets.transcription_models_status(config),
        "embedding": search.embedder_status(),
    }


def provision_local(config: Config, progress: Progress | None = None) -> None:
    """Download all local models and select fully-local mode — the CLI
    `setup` Local path. Idempotent; raises ConfigError on failure."""
    download_local_models(config, progress=progress)
    save_config(
        {
            "backend": "local",
            "transcribe": "local",
            "local_model": config.local_model,
        }
    )


def prefetch_embedding_model(progress: Progress | None = None) -> None:
    """Best-effort download of the search embedding model at setup time, so
    the first capture/search doesn't pay for it. Search degrades to FTS-only
    without it, so a failure here is reported by doctor, never raised."""
    from vetromar import search

    if progress:
        progress("Fetching search embedding model", None, None)
    try:
        search.prefetch_model()
    except search.EmbedderUnavailableError:
        pass


def health_report(config: Config, *, check_api: bool = False) -> dict:
    """Structured preflight for the selected backend — the data behind `doctor`.

    Returns {backend, ready, checks: [{ok, label, detail}]}. Never raises: a
    probe that fails becomes an `ok: False` check so both `doctor` and the UI
    can render a full report."""
    from vetromar.config import config_file_path

    checks: list[dict] = []

    def add(ok: bool, label: str, detail: str = "") -> None:
        checks.append({"ok": bool(ok), "label": label, "detail": detail})

    cfg_path = config_file_path()
    add(cfg_path.exists(), "config file", str(cfg_path))

    ready = True
    if config.backend == "api" and config.ai_provider == "openai":
        if config.openai_base_url:
            add(True, "AI provider (OpenAI-compatible)", config.openai_base_url)
        else:
            add(False, "AI provider (OpenAI-compatible)",
                "no endpoint configured — set the base URL in Settings")
            ready = False
        if check_api and config.openai_base_url:
            from vetromar.ai import get_provider
            from vetromar.providers import CredentialsRejected

            try:
                get_provider(config).check_credentials()
                add(True, "endpoint accepted the credentials (live check)")
            except CredentialsRejected:
                add(False, "endpoint rejected the API key (live check)")
                ready = False
            except ConfigError as exc:
                add(False, "endpoint unreachable (live check)", exc.message)
                ready = False
    elif config.backend == "api":
        has_key = bool(config.api_key)
        if has_key:
            add(True, "Anthropic API key present")
        else:
            add(False, "AI access",
                "add an AI provider in Settings, or set ANTHROPIC_API_KEY")
            ready = False
        if check_api and has_key:
            import anthropic

            try:
                anthropic.Anthropic(api_key=config.api_key).models.list(limit=1)
                add(True, "API key accepted (live check)")
            except anthropic.AuthenticationError:
                add(False, "API key rejected (live check)")
                ready = False
    elif config.backend == "local":
        from vetromar import runtime

        st = runtime.local_status(config)
        add(st.binary, "Ollama runtime installed")
        add(st.server, "Ollama server running", config.ollama_host)
        add(st.model, "local model pulled", config.local_model)
        ready = st.ready
    else:
        add(False, f"unknown backend {config.backend!r}")
        ready = False

    # Transcription tier. In auto mode a missing Deepgram key just means the
    # local tier — never blocks readiness. Explicit cloud without a key does.
    from vetromar.transcription.base import resolve_transcription_mode

    try:
        transcription = resolve_transcription_mode(config)
    except ValueError:
        transcription = "local"
        add(False, f"unknown transcription mode {config.transcribe!r}")
        ready = False
    else:
        provider = "Deepgram" if transcription == "cloud" else "WhisperX on this machine"
        add(True, "transcription mode", f"{transcription} ({provider})")
        if transcription == "cloud":
            has_dg_key = bool(config.deepgram_api_key)
            if has_dg_key:
                add(True, "Deepgram API key present")
                if check_api:
                    ok = _check_deepgram_key(config.deepgram_api_key)
                    add(ok, f"Deepgram key {'accepted' if ok else 'rejected'} (live check)")
                    if not ok:
                        ready = False
            else:
                add(False, "cloud transcription access",
                    "add a Deepgram API key in Settings, or set DEEPGRAM_API_KEY")
                if config.transcribe == "cloud":
                    ready = False
        else:
            from vetromar.transcription.assets import (
                missing_component_names,
                transcription_models_status,
            )

            weights = transcription_models_status(config)
            if weights["present"]:
                add(True, "local transcription models downloaded")
            else:
                missing = ", ".join(missing_component_names(weights))
                add(False, "local transcription models",
                    f"not downloaded ({missing}) — Settings → Download local models")
                ready = False

    try:
        config.ensure_dirs()
        add(True, "store dir writable", str(config.db_path.parent))
    except OSError as exc:
        add(False, "store dir not writable", str(exc))
        ready = False

    # Informational — search works FTS-only without the model, so this never
    # flips `ready`.
    from vetromar import search

    embed = search.embedder_status()
    add(embed["cached"], "search embedding model cached", embed["model"])

    from vetromar import __version__

    return {
        "backend": config.backend,
        "transcription": transcription,
        "ready": ready,
        "app_version": __version__,
        "checks": checks,
        # Per-component "downloaded?" snapshot for the Settings local-models
        # section — reported regardless of the active backend.
        "local_models": local_models_status(config),
    }
