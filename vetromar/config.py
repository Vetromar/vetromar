"""Configuration.

The one load-bearing switch: `backend` selects which extraction backend runs.
Privacy is the customer's choice — "api" (frontier model, best quality) or
"local" (Ollama, data never leaves the machine). Everything upstream and
downstream of extraction is identical in both modes.

Resolution order for every field is **env > config.toml > default**. Bare
`Config()` reads env-or-default (the pre-M5 behavior, kept for direct callers
and tests); `load_config()` additionally layers `~/.vetromar/config.toml`, which
`vetromar setup` writes so the customer's choice persists across runs. The API
key is a secret and never lands in config.toml — it lives env-first, then a
0600 `~/.vetromar/credentials` file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


VETROMAR_HOME = Path.home() / ".vetromar"
DEFAULT_DB_PATH = VETROMAR_HOME / "store.db"
DEFAULT_RUNTIME_DIR = VETROMAR_HOME / "runtime"
DEFAULT_CONFIG_PATH = VETROMAR_HOME / "config.toml"
CREDENTIALS_PATH = VETROMAR_HOME / "credentials"
# Per-provider secret files, one secret each (same idiom as CREDENTIALS_PATH —
# no multi-key store to migrate).
DEEPGRAM_CREDENTIALS_PATH = VETROMAR_HOME / "credentials-deepgram"
OPENAI_CREDENTIALS_PATH = VETROMAR_HOME / "credentials-openai"
# Workspace session (M14): the cloud auth token (0600) and the local session
# cache (workspace name/role/device_id — non-secret JSON). Env-overridable so
# a second scratch "device" can run on the same machine (tests, verify).
CLOUD_CREDENTIALS_PATH = VETROMAR_HOME / "credentials-cloud"
WORKSPACE_CACHE_PATH = VETROMAR_HOME / "workspace.json"


def cloud_credentials_path() -> Path:
    return Path(os.environ.get("VETROMAR_CLOUD_CREDENTIALS", str(CLOUD_CREDENTIALS_PATH)))


def workspace_cache_path() -> Path:
    return Path(os.environ.get("VETROMAR_WORKSPACE_CACHE", str(WORKSPACE_CACHE_PATH)))

# Ollama release pinned for reproducible managed installs (see runtime/binary.py).
DEFAULT_OLLAMA_VERSION = "v0.31.2"

# Vetromar's managed Ollama server runs on a DEDICATED port, not Ollama's
# default 11434. This is deliberate isolation: whether the user has their own
# Ollama installed/running must not change Vetromar's behavior, so we never
# collide with or reuse a server on the standard port. Read from a Vetromar-
# specific env var (never the generic OLLAMA_HOST, which an Ollama user may have
# set to point at their own instance).
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11435"


def _as_bool(value: Any) -> bool:
    """Cast env/TOML values to bool. Load-bearing for env vars: `_resolve`
    passes the raw string through `cast`, and bool("false") is True."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def model_supports_adaptive_thinking(model: str) -> bool:
    """Whether the Anthropic model accepts thinking={'type': 'adaptive'}.

    Haiku-tier (and pre-4.6) models reject it with a 400 — callers must omit
    the thinking parameter entirely for those. Keyed on the tier name so a
    future haiku release stays safe without a code change."""
    return "haiku" not in model


@dataclass
class Config:
    # "api" | "local" — the pluggable extraction seam, selected per customer
    backend: str = field(default_factory=lambda: os.environ.get("VETROMAR_BACKEND", "api"))

    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("VETROMAR_DB", str(DEFAULT_DB_PATH)))
    )

    # API mode: the model name on whichever provider is selected.
    # Overridable, never hardcoded downstream.
    api_model: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_API_MODEL", "claude-haiku-4-5")
    )

    # API key — resolved env ANTHROPIC_API_KEY > credentials file > None. Secret:
    # never persisted to config.toml. Threaded into ApiBackend explicitly so a
    # missing key becomes a friendly ConfigError instead of a raw SDK exception.
    api_key: str | None = field(default_factory=lambda: _load_api_key())

    # BYO provider selection: "anthropic" (native SDK) or "openai" (any
    # OpenAI-compatible endpoint — OpenAI, OpenRouter, Groq, Ollama's /v1,
    # LM Studio, vLLM, a LiteLLM proxy). `api_model` is shared: it names a
    # model on whichever provider is selected.
    ai_provider: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_AI_PROVIDER", "anthropic")
    )
    # OpenAI-compatible endpoint base URL, e.g. https://api.openai.com/v1 or
    # http://localhost:11434/v1. Not a secret — persists in config.toml.
    openai_base_url: str | None = field(
        default_factory=lambda: os.environ.get("VETROMAR_OPENAI_BASE_URL") or None
    )
    # OpenAI-compatible key — env OPENAI_API_KEY > credentials-openai file >
    # None. Secret: never persisted to config.toml. Optional: local endpoints
    # (Ollama, LM Studio) need none.
    openai_api_key: str | None = field(default_factory=lambda: _load_openai_api_key())

    # Local mode: Ollama model tag. RAM tiers (see CLAUDE.md for measurements):
    # <12 GB: no viable local tier — use VETROMAR_BACKEND=api;
    # 16–24 GB: qwen3.5:9b (default); ≥32 GB: qwen3.5:27b (overnight tier on less).
    local_model: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_LOCAL_MODEL", "qwen3.5:9b")
    )

    # Sampling seed for local extraction. Fixed by default for reproducible
    # prompt iteration; override to make a re-run an actual retry
    # (the v0 retry story) instead of a deterministic repeat.
    local_seed: int = field(
        default_factory=lambda: int(os.environ.get("VETROMAR_LOCAL_SEED", "42"))
    )

    # Managed Ollama runtime (M5, zero-install local mode). runtime_dir holds the
    # vendored `ollama` binary + a models/ dir; ollama_host is where the server
    # (managed or system) answers; ollama_version pins the download.
    runtime_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("VETROMAR_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR))
        )
    )
    ollama_host: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    )
    ollama_version: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_OLLAMA_VERSION", DEFAULT_OLLAMA_VERSION)
    )

    # Transcription / diarization (capture extra)
    whisper_model: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_WHISPER_MODEL", "large-v3")
    )
    # Ungated mirror of pyannote's community-1 pipeline (CC-BY-4.0) — downloads
    # without any HF account, which keeps customer setup token-free. The shipped
    # product bundles these weights; HF is a build-time source, never a customer
    # dependency. HF_TOKEN is only needed if this is overridden to a gated repo.
    diarization_model: str = field(
        default_factory=lambda: os.environ.get(
            "VETROMAR_DIARIZATION_MODEL",
            "pyannote-community/speaker-diarization-community-1",
        )
    )
    hf_token: str | None = field(default_factory=lambda: os.environ.get("HF_TOKEN"))

    # Transcription tier: "auto" | "local" | "cloud". Local = whisperx+pyannote
    # on this machine (the privacy tier); cloud = Deepgram batch STT with
    # diarization (the fast tier — audio leaves the machine). "auto" resolves
    # to cloud iff a Deepgram key is configured, local otherwise (see
    # transcription/base.py:resolve_transcription_mode).
    transcribe: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_TRANSCRIBE", "auto")
    )
    # Deepgram key — env DEEPGRAM_API_KEY > credentials-deepgram file > None.
    # Secret: never persisted to config.toml. Optional: absent means the fast
    # tier is simply off.
    deepgram_api_key: str | None = field(default_factory=lambda: _load_deepgram_api_key())
    deepgram_model: str = field(
        default_factory=lambda: os.environ.get("VETROMAR_DEEPGRAM_MODEL", "nova-3")
    )

    # Background auto-sync (ui_server scheduler). Off by default — auto-sync
    # spends API credits; it's one toggle in Settings.
    auto_sync_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("VETROMAR_AUTO_SYNC_ENABLED", ""))
    )
    auto_sync_interval_minutes: int = field(
        default_factory=lambda: int(os.environ.get("VETROMAR_AUTO_SYNC_INTERVAL_MINUTES", "60"))
    )

    # First-run onboarding (desktop UI). Independent flags: replaying the tour
    # must not resurrect a dismissed checklist, and vice versa.
    onboarding_tour_done: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("VETROMAR_ONBOARDING_TOUR_DONE", ""))
    )
    onboarding_checklist_dismissed: bool = field(
        default_factory=lambda: _as_bool(
            os.environ.get("VETROMAR_ONBOARDING_CHECKLIST_DISMISSED", "")
        )
    )

    # Workspace server (self-hosted): where sign-in and multi-device sync
    # point. Default = a server on this machine (`python -m cloud` /
    # docker compose); teams set their own server's URL in the app's sign-in
    # screen (persists here via config.toml).
    cloud_api_url: str = field(
        default_factory=lambda: os.environ.get(
            "VETROMAR_CLOUD_API_URL", "http://localhost:8787"
        )
    )
    # Where invite links point (the static website with invite-accept.html).
    website_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "VETROMAR_WEBSITE_URL", "https://vetromar.com"
        )
    )
    workspace_sync_interval_minutes: int = field(
        default_factory=lambda: int(
            os.environ.get("VETROMAR_WORKSPACE_SYNC_INTERVAL_MINUTES", "5")
        )
    )
    # Cloud session token — env VETROMAR_CLOUD_TOKEN > credentials-cloud file
    # > None. Secret: never persisted to config.toml.
    cloud_token: str | None = field(default_factory=lambda: _load_cloud_token())

    @property
    def models_dir(self) -> Path:
        """OLLAMA_MODELS for the managed server — weights live under Vetromar."""
        return self.runtime_dir / "models"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)


# -- resolution helpers -------------------------------------------------------


def config_file_path() -> Path:
    """The config.toml path in effect (VETROMAR_CONFIG override or default)."""
    return Path(os.environ.get("VETROMAR_CONFIG", str(DEFAULT_CONFIG_PATH)))


# Back-compat internal alias.
_config_file_path = config_file_path


def _load_config_file(path: Path) -> dict[str, Any]:
    """Read config.toml if present. A missing or malformed file is not fatal —
    setup hasn't run yet, or was hand-edited; fall through to env/defaults."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _load_api_key() -> str | None:
    """env ANTHROPIC_API_KEY > credentials file > None."""
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    if CREDENTIALS_PATH.exists():
        key = CREDENTIALS_PATH.read_text().strip()
        if key:
            return key
    return None


def _load_cloud_token() -> str | None:
    """env VETROMAR_CLOUD_TOKEN > credentials-cloud file > None."""
    env = os.environ.get("VETROMAR_CLOUD_TOKEN")
    if env:
        return env
    path = cloud_credentials_path()
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    return None


def _load_openai_api_key() -> str | None:
    """env OPENAI_API_KEY > credentials-openai file > None."""
    env = os.environ.get("OPENAI_API_KEY")
    if env:
        return env
    if OPENAI_CREDENTIALS_PATH.exists():
        key = OPENAI_CREDENTIALS_PATH.read_text().strip()
        if key:
            return key
    return None


def _load_deepgram_api_key() -> str | None:
    """env DEEPGRAM_API_KEY > credentials-deepgram file > None."""
    env = os.environ.get("DEEPGRAM_API_KEY")
    if env:
        return env
    if DEEPGRAM_CREDENTIALS_PATH.exists():
        key = DEEPGRAM_CREDENTIALS_PATH.read_text().strip()
        if key:
            return key
    return None


def _resolve(
    env_key: str,
    file_cfg: dict[str, Any],
    file_key: str,
    default: Any,
    cast: Callable[[Any], Any] = lambda x: x,
) -> Any:
    """env > config.toml > default. Env/file values pass through `cast`."""
    env_val = os.environ.get(env_key)
    if env_val:
        return cast(env_val)
    if file_cfg.get(file_key) is not None:
        return cast(file_cfg[file_key])
    return default


def load_config() -> Config:
    """Build a Config layering config.toml between env (highest) and defaults."""
    file_cfg = _load_config_file(_config_file_path())
    return Config(
        backend=_resolve("VETROMAR_BACKEND", file_cfg, "backend", "api"),
        db_path=_resolve("VETROMAR_DB", file_cfg, "db_path", DEFAULT_DB_PATH, cast=Path),
        api_model=_resolve("VETROMAR_API_MODEL", file_cfg, "api_model", "claude-haiku-4-5"),
        api_key=_load_api_key(),
        ai_provider=_resolve("VETROMAR_AI_PROVIDER", file_cfg, "ai_provider", "anthropic"),
        openai_base_url=_resolve(
            "VETROMAR_OPENAI_BASE_URL", file_cfg, "openai_base_url", None
        ),
        openai_api_key=_load_openai_api_key(),
        local_model=_resolve("VETROMAR_LOCAL_MODEL", file_cfg, "local_model", "qwen3.5:9b"),
        local_seed=_resolve("VETROMAR_LOCAL_SEED", file_cfg, "local_seed", 42, cast=int),
        runtime_dir=_resolve(
            "VETROMAR_RUNTIME_DIR", file_cfg, "runtime_dir", DEFAULT_RUNTIME_DIR, cast=Path
        ),
        ollama_host=_resolve(
            "VETROMAR_OLLAMA_HOST", file_cfg, "ollama_host", DEFAULT_OLLAMA_HOST
        ),
        ollama_version=_resolve(
            "VETROMAR_OLLAMA_VERSION", file_cfg, "ollama_version", DEFAULT_OLLAMA_VERSION
        ),
        whisper_model=_resolve("VETROMAR_WHISPER_MODEL", file_cfg, "whisper_model", "large-v3"),
        diarization_model=_resolve(
            "VETROMAR_DIARIZATION_MODEL",
            file_cfg,
            "diarization_model",
            "pyannote-community/speaker-diarization-community-1",
        ),
        hf_token=os.environ.get("HF_TOKEN"),
        transcribe=_resolve("VETROMAR_TRANSCRIBE", file_cfg, "transcribe", "auto"),
        deepgram_api_key=_load_deepgram_api_key(),
        deepgram_model=_resolve("VETROMAR_DEEPGRAM_MODEL", file_cfg, "deepgram_model", "nova-3"),
        auto_sync_enabled=_resolve(
            "VETROMAR_AUTO_SYNC_ENABLED", file_cfg, "auto_sync_enabled", False, cast=_as_bool
        ),
        onboarding_tour_done=_resolve(
            "VETROMAR_ONBOARDING_TOUR_DONE", file_cfg, "onboarding_tour_done", False, cast=_as_bool
        ),
        onboarding_checklist_dismissed=_resolve(
            "VETROMAR_ONBOARDING_CHECKLIST_DISMISSED",
            file_cfg,
            "onboarding_checklist_dismissed",
            False,
            cast=_as_bool,
        ),
        auto_sync_interval_minutes=_resolve(
            "VETROMAR_AUTO_SYNC_INTERVAL_MINUTES",
            file_cfg,
            "auto_sync_interval_minutes",
            60,
            cast=int,
        ),
        cloud_api_url=_resolve(
            "VETROMAR_CLOUD_API_URL", file_cfg, "cloud_api_url", "http://localhost:8787"
        ),
        website_base_url=_resolve(
            "VETROMAR_WEBSITE_URL", file_cfg, "website_base_url", "https://vetromar.com"
        ),
        workspace_sync_interval_minutes=_resolve(
            "VETROMAR_WORKSPACE_SYNC_INTERVAL_MINUTES",
            file_cfg,
            "workspace_sync_interval_minutes",
            5,
            cast=int,
        ),
        cloud_token=_load_cloud_token(),
    )


# -- persistence (written by `vetromar setup`) --------------------------------


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_config(updates: dict[str, Any]) -> Path:
    """Merge `updates` into config.toml and rewrite it.

    Flat key/scalar set only (backend, local_model, ...) — hand-rolled writer to
    avoid a serializer dependency. Never write the API key here; use
    save_api_key(). Returns the config path."""
    path = _config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**_load_config_file(path), **updates}
    lines = ["# Vetromar config — written by `vetromar setup` (env vars override).\n"]
    lines += [f"{key} = {_toml_scalar(value)}\n" for key, value in merged.items()]
    path.write_text("".join(lines))
    return path


def save_api_key(key: str) -> Path:
    """Persist the API key to a 0600 credentials file (never config.toml)."""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(key.strip() + "\n")
    CREDENTIALS_PATH.chmod(0o600)
    return CREDENTIALS_PATH


def save_cloud_token(token: str) -> Path:
    """Persist the workspace session token to its own 0600 file."""
    path = cloud_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n")
    path.chmod(0o600)
    return path


def clear_cloud_token() -> None:
    path = cloud_credentials_path()
    if path.exists():
        path.unlink()


def save_openai_api_key(key: str) -> Path:
    """Persist the OpenAI-compatible key to its own 0600 file (never config.toml)."""
    OPENAI_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAI_CREDENTIALS_PATH.write_text(key.strip() + "\n")
    OPENAI_CREDENTIALS_PATH.chmod(0o600)
    return OPENAI_CREDENTIALS_PATH


def save_deepgram_api_key(key: str) -> Path:
    """Persist the Deepgram key to its own 0600 file (never config.toml)."""
    DEEPGRAM_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEEPGRAM_CREDENTIALS_PATH.write_text(key.strip() + "\n")
    DEEPGRAM_CREDENTIALS_PATH.chmod(0o600)
    return DEEPGRAM_CREDENTIALS_PATH
