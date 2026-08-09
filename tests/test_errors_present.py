"""present_error — the desktop app's user-facing error presenter.

Expected/actionable errors pass through untouched (the frontend matches some
of them literally); everything else collapses to a generic message + VM-* code.
"""

import httpx
import pytest

from vetromar.errors import ConfigError, present_error
from vetromar.extraction.validate import EvidenceMismatchError, GroundedQuoteError
from vetromar.operations import InvalidApiKey
from vetromar.workspace.client import NotSignedIn, WorkspaceError


def test_config_error_passes_through_with_hint():
    exc = ConfigError("No API backend selected.", hint="Open Settings.")
    message, code = present_error(exc)
    assert code is None
    assert message == "No API backend selected.\n  → Open Settings."


@pytest.mark.parametrize(
    "exc",
    [
        NotSignedIn("invalid or expired token"),
        WorkspaceError("invalid email or password"),
        InvalidApiKey("That key was rejected by Anthropic."),
    ],
)
def test_expected_errors_pass_through_byte_identical(exc):
    message, code = present_error(exc)
    assert code is None
    assert message == str(exc)


def test_cancelled_config_error_keeps_cancelled_text():
    # Sources.svelte special-cases /cancelled/i on the message.
    exc = ConfigError("Authorization cancelled.", hint="Run connect again to retry.")
    message, code = present_error(exc)
    assert code is None
    assert "cancelled" in message.lower()


@pytest.mark.parametrize(
    "exc",
    [
        EvidenceMismatchError(["we weren't sure about Mhmm. That decision"]),
        GroundedQuoteError(["some transcript fragment"]),
    ],
)
def test_gate_errors_become_vm200_without_transcript_fragments(exc):
    message, code = present_error(exc)
    assert code == "VM-200"
    assert "VM-200" in message
    assert "transcript fragment" not in message
    assert "Mhmm" not in message
    assert "correctness bug" not in message


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("[Errno 61] connection refused to 127.0.0.1:8787"),
        ConnectionRefusedError("[Errno 61] connection refused"),
        TimeoutError("timed out"),
    ],
)
def test_network_errors_become_vm300(exc):
    message, code = present_error(exc)
    assert code == "VM-300"
    assert "connection" in message.lower()
    assert "127.0.0.1" not in message
    assert "Errno" not in message


def test_unexpected_errors_become_generic_vm100():
    message, code = present_error(RuntimeError("psycopg OperationalError: ssl bad"))
    assert code == "VM-100"
    assert message == "Something went wrong. Please try again. (error VM-100)"
    assert "psycopg" not in message
