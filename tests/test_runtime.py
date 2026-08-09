"""Managed Ollama runtime — decision logic with the side effects mocked.

The provisioning *orchestration* (nothing installed → download → serve → pull)
is verified here; the real from-scratch binary download is a network path
exercised manually (this machine already has a system Ollama). See the M5 plan's
verification section.
"""

import stat

import pytest

from vetromar import runtime
from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.runtime import binary, model, server


def _config(tmp_path, **kw):
    return Config(
        runtime_dir=tmp_path,
        ollama_host="http://127.0.0.1:11434",
        ollama_version="v0.31.2",
        local_model="qwen3.5:9b",
        **kw,
    )


# -- binary resolution --------------------------------------------------------


def test_resolve_returns_managed_binary(tmp_path, monkeypatch):
    managed = tmp_path / "ollama"
    managed.write_text("#!/bin/sh\n")
    managed.chmod(managed.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(binary.shutil, "which", lambda _: "/usr/local/bin/ollama")
    assert binary.resolve_binary(_config(tmp_path)) == managed


def test_resolve_ignores_system_ollama_on_supported_platform(tmp_path, monkeypatch):
    # Full isolation: a system Ollama on PATH must NOT be used when we can bundle
    # our own — its presence cannot change behavior.
    monkeypatch.setattr(binary, "_platform_supported", lambda: True)
    monkeypatch.setattr(binary.shutil, "which", lambda _: "/usr/local/bin/ollama")
    assert binary.resolve_binary(_config(tmp_path)) is None


def test_resolve_uses_system_only_on_unsupported_platform(tmp_path, monkeypatch):
    # The one PATH fallback: a platform we don't ship a binary for.
    monkeypatch.setattr(binary, "_platform_supported", lambda: False)
    monkeypatch.setattr(binary.shutil, "which", lambda _: "/usr/local/bin/ollama")
    assert str(binary.resolve_binary(_config(tmp_path))) == "/usr/local/bin/ollama"


def test_resolve_none_when_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(binary, "_platform_supported", lambda: True)
    monkeypatch.setattr(binary.shutil, "which", lambda _: None)
    assert binary.resolve_binary(_config(tmp_path)) is None


def test_platform_archive_maps_known(tmp_path, monkeypatch):
    monkeypatch.setattr(binary.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(binary.platform, "machine", lambda: "arm64")
    asset, url = binary.platform_archive(_config(tmp_path))
    assert asset == "ollama-darwin.tgz"
    assert url.endswith("/v0.31.2/ollama-darwin.tgz")


def test_platform_archive_unsupported_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(binary.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(binary.platform, "machine", lambda: "sparc")
    with pytest.raises(ConfigError):
        binary.platform_archive(_config(tmp_path))


# -- server -------------------------------------------------------------------


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_server_up_true(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResp())
    assert server.server_up(_config(tmp_path)) is True


def test_server_up_false_on_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert server.server_up(_config(tmp_path)) is False


def test_ensure_server_reuses_running(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "server_up", lambda c: True)

    def fail(*a, **k):
        raise AssertionError("should not spawn a server when one is up")

    monkeypatch.setattr(server.subprocess, "Popen", fail)
    server.ensure_server(_config(tmp_path))  # no raise


def test_ensure_server_no_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "server_up", lambda c: False)
    monkeypatch.setattr(server, "resolve_binary", lambda c: None)
    with pytest.raises(ConfigError):
        server.ensure_server(_config(tmp_path))


# -- model --------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, name):
        self.model = name


class _FakeClient:
    def __init__(self, names, pulled=None):
        self._names = names
        self._pulled = pulled if pulled is not None else []

    def list(self):
        return type("L", (), {"models": [_FakeEntry(n) for n in self._names]})()

    def pull(self, name, stream=True):
        self._pulled.append(name)
        return iter([type("C", (), {"status": "pulling", "completed": 1, "total": 2})()])


def test_model_present_true(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "_client", lambda c: _FakeClient(["qwen3.5:9b", "llama3.1:8b"]))
    assert model.model_present(_config(tmp_path)) is True


def test_model_present_false(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "_client", lambda c: _FakeClient(["llama3.1:8b"]))
    assert model.model_present(_config(tmp_path)) is False


def test_ensure_model_idempotent_when_present(tmp_path, monkeypatch):
    pulled = []
    monkeypatch.setattr(
        model, "_client", lambda c: _FakeClient(["qwen3.5:9b"], pulled=pulled)
    )
    model.ensure_model(_config(tmp_path))
    assert pulled == []  # already present → no pull


def test_ensure_model_pulls_when_absent(tmp_path, monkeypatch):
    pulled = []
    monkeypatch.setattr(model, "_client", lambda c: _FakeClient([], pulled=pulled))
    model.ensure_model(_config(tmp_path))
    assert pulled == ["qwen3.5:9b"]


# -- orchestration ------------------------------------------------------------


def test_ensure_local_ready_provisions_when_absent(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "resolve_binary", lambda c: None)
    monkeypatch.setattr(runtime, "download_binary", lambda c, p=None: calls.append("download"))
    monkeypatch.setattr(runtime, "ensure_server", lambda c: calls.append("serve"))
    monkeypatch.setattr(runtime, "ensure_model", lambda c, p=None: calls.append("pull"))
    runtime.ensure_local_ready(_config(tmp_path))
    assert calls == ["download", "serve", "pull"]


def test_ensure_local_ready_skips_download_when_present(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "resolve_binary", lambda c: tmp_path / "ollama")
    monkeypatch.setattr(runtime, "download_binary", lambda c, p=None: calls.append("download"))
    monkeypatch.setattr(runtime, "ensure_server", lambda c: calls.append("serve"))
    monkeypatch.setattr(runtime, "ensure_model", lambda c, p=None: calls.append("pull"))
    runtime.ensure_local_ready(_config(tmp_path))
    assert calls == ["serve", "pull"]


def test_local_status_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "resolve_binary", lambda c: tmp_path / "ollama")
    monkeypatch.setattr(runtime, "server_up", lambda c: True)
    monkeypatch.setattr(runtime, "model_present", lambda c: False)
    st = runtime.local_status(_config(tmp_path))
    assert (st.binary, st.server, st.model) == (True, True, False)
    assert st.ready is False
