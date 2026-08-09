"""The customer's connected sources — `~/.vetromar/sources.toml`.

One table per source; a source is EITHER remote (`url`, streamable HTTP,
spec-standard OAuth) or local (`command` + `args` + `env`, stdio — the
power-user/self-hosted path and the test path). `tools` optionally narrows
which of the server's tools the sync agent may call. Secrets for stdio
servers are the customer's own env values passed through at spawn; Vetromar
never stores them (remote-source tokens live in `~/.vetromar/tokens/`,
written by the OAuth flow, not by hand).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from vetromar.config import VETROMAR_HOME
from vetromar.errors import ConfigError


class SourceConfig(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    source_kind: str = "document"
    enabled: bool = True
    url: Optional[str] = None
    command: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    tools: Optional[list[str]] = None

    @model_validator(mode="after")
    def _one_transport(self) -> "SourceConfig":
        if bool(self.url) == bool(self.command):
            raise ValueError(f"source '{self.name}' needs exactly one of url/command")
        return self

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


def sources_file_path() -> Path:
    return VETROMAR_HOME / "sources.toml"


def load_sources(path: Path | None = None) -> list[SourceConfig]:
    path = path or sources_file_path()
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Could not parse {path}: {exc}",
            hint="Fix the TOML by hand or remove the file and re-run `vetromar connect`.",
        ) from exc
    sources = []
    for name, table in data.get("sources", {}).items():
        try:
            sources.append(SourceConfig(name=name, **table))
        except ValueError as exc:
            raise ConfigError(f"Invalid source '{name}' in {path}: {exc}") from exc
    return sources


def get_source(name: str, path: Path | None = None) -> SourceConfig:
    for source in load_sources(path):
        if source.name == name:
            return source
    raise ConfigError(
        f"No source named '{name}' is connected.",
        hint="`vetromar sources list` shows connected sources; `vetromar connect` adds one.",
    )


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"unsupported TOML value: {value!r}")


def save_sources(sources: list[SourceConfig], path: Path | None = None) -> Path:
    path = path or sources_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Vetromar connected sources — managed by `vetromar connect`.", ""]
    for source in sources:
        lines.append(f"[sources.{source.name}]")
        for key, value in source.model_dump(exclude={"name"}, exclude_none=True).items():
            if value in ([], {}):
                continue
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    path.write_text("\n".join(lines))
    return path


def upsert_source(source: SourceConfig, path: Path | None = None) -> Path:
    sources = [s for s in load_sources(path) if s.name != source.name]
    sources.append(source)
    return save_sources(sources, path)


def remove_source(name: str, path: Path | None = None) -> Path:
    sources = load_sources(path)
    remaining = [s for s in sources if s.name != name]
    if len(remaining) == len(sources):
        raise ConfigError(f"No source named '{name}' is connected.")
    return save_sources(remaining, path)
