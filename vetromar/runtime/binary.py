"""Provisioning the Ollama binary — the zero-install half of local mode.

The customer never runs an Ollama installer. `resolve_binary` returns ONLY the
Vetromar-managed binary under `runtime_dir` on platforms we bundle for — any
system Ollama on PATH is deliberately ignored so its presence can't change
behavior. When the managed binary is missing, `download_binary` fetches the
pinned release archive from GitHub (stdlib urllib, no new dependency), extracts
it into `runtime_dir`, and smoke-tests it. `platform_archive` maps the current
OS/arch to a release asset; unsupported platforms raise a clear ConfigError.

The only PATH fallback is on a platform we don't ship a binary for, where a
system Ollama is the user's sole option (a documented manual fallback).

Note: even on a machine with a system Ollama, `resolve_binary` returns None until
the managed binary is downloaded, so `setup` provisions our own. The from-scratch
download path is exercised by forcing it into a temp runtime_dir (see tests).
"""

from __future__ import annotations

import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from vetromar.errors import ConfigError

# progress(label, completed_bytes_or_None, total_bytes_or_None)
Progress = Callable[[str, "int | None", "int | None"], None]

_RELEASE_BASE = "https://github.com/ollama/ollama/releases/download"

# (system, machine) -> release asset filename. machine is normalized first.
_ASSETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "ollama-darwin.tgz",
    ("darwin", "amd64"): "ollama-darwin.tgz",
    ("linux", "amd64"): "ollama-linux-amd64.tgz",
    ("linux", "arm64"): "ollama-linux-arm64.tgz",
    ("windows", "amd64"): "ollama-windows-amd64.zip",
}

_MACHINE_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def _managed_binary_path(config) -> Path:
    name = "ollama.exe" if platform.system().lower() == "windows" else "ollama"
    return Path(config.runtime_dir) / name


def _platform_supported() -> bool:
    """True if we ship a bundled Ollama binary for this OS/arch."""
    system = platform.system().lower()
    machine = _MACHINE_ALIASES.get(platform.machine().lower())
    return bool(machine and (system, machine) in _ASSETS)


def resolve_binary(config) -> Path | None:
    """The Vetromar-managed binary under runtime_dir, or None.

    We deliberately IGNORE any system Ollama on PATH: whether the user has Ollama
    installed must not change Vetromar's behavior, so on platforms we bundle for
    we always run our own pinned binary (downloaded on first `setup`). The lone
    exception is a platform we don't ship a binary for, where a system Ollama is
    the only option and stays a documented manual fallback."""
    managed = _managed_binary_path(config)
    if managed.exists() and _is_executable(managed):
        return managed
    if not _platform_supported():
        system = shutil.which("ollama")
        return Path(system) if system else None
    return None


def _is_executable(path: Path) -> bool:
    return path.is_file() and bool(path.stat().st_mode & stat.S_IXUSR)


def platform_archive(config) -> tuple[str, str]:
    """(asset filename, download URL) for this OS/arch and the pinned version."""
    system = platform.system().lower()
    machine = _MACHINE_ALIASES.get(platform.machine().lower())
    asset = _ASSETS.get((system, machine)) if machine else None
    if asset is None:
        raise ConfigError(
            f"No bundled Ollama runtime for {platform.system()}/{platform.machine()}.",
            hint="Install Ollama manually from https://ollama.com/download, then re-run `vetromar setup`.",
        )
    return asset, f"{_RELEASE_BASE}/{config.ollama_version}/{asset}"


def download_binary(config, progress: Progress | None = None) -> Path:
    """Download + extract the pinned Ollama release into runtime_dir.

    Returns the path to the extracted `ollama` binary. Raises ConfigError on
    network/extract failure so the CLI reports it cleanly."""
    asset, url = platform_archive(config)
    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        try:
            _stream_download(url, archive, progress)
            _extract(archive, runtime_dir, progress)
        except (OSError, urllib.error.URLError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise ConfigError(
                f"Could not download the Ollama runtime ({exc}).",
                hint="Check your connection, or install Ollama manually from https://ollama.com/download.",
            ) from exc

    binary = _locate_binary(runtime_dir)
    if binary is None:
        raise ConfigError(
            f"Downloaded {asset} but found no `ollama` binary inside it.",
            hint="Install Ollama manually from https://ollama.com/download.",
        )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    _smoke_test(binary)
    return binary


def _stream_download(url: str, dest: Path, progress: Progress | None) -> None:
    with urllib.request.urlopen(url) as resp:  # noqa: S310 — pinned GitHub release URL
        total = int(resp.headers.get("Content-Length") or 0) or None
        done = 0
        with dest.open("wb") as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress("Downloading Ollama runtime", done, total)


def _extract(archive: Path, dest: Path, progress: Progress | None) -> None:
    if progress:
        progress("Extracting Ollama runtime", None, None)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")


def _locate_binary(runtime_dir: Path) -> Path | None:
    """Find the `ollama` binary in the extracted tree (root or bin/)."""
    names = {"ollama", "ollama.exe"}
    direct = _managed_binary_path_from(runtime_dir)
    if direct.exists():
        return direct
    for candidate in runtime_dir.rglob("*"):
        if candidate.is_file() and candidate.name in names:
            return candidate
    return None


def _managed_binary_path_from(runtime_dir: Path) -> Path:
    name = "ollama.exe" if platform.system().lower() == "windows" else "ollama"
    return runtime_dir / name


def _smoke_test(binary: Path) -> None:
    try:
        subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ConfigError(
            f"The downloaded Ollama binary did not run ({exc}).",
            hint="Install Ollama manually from https://ollama.com/download.",
        ) from exc
