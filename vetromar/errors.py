"""User-facing configuration/setup errors.

A `ConfigError` means the failure is the *setup*, not the code: a missing API
key, an unreachable local runtime, a model that was never pulled. These are the
seams a first-time user hits before anything works, so they carry a plain
message plus the one command that fixes it. The CLI catches these at the app
level and prints them without a traceback.
"""

from __future__ import annotations


class ConfigError(Exception):
    """A setup problem the user can fix, with a suggested next command.

    Raise this instead of letting a raw SDK/connection exception surface. The
    `hint` is the concrete command to run (e.g. ``vetromar setup``); the CLI
    renders message + hint as a clean error.
    """

    def __init__(self, message: str, hint: str | None = None):
        self.message = message
        self.hint = hint
        super().__init__(message if hint is None else f"{message}\n  → {hint}")


# -- user-facing presentation of unexpected errors ---------------------------
#
# Support-facing error codes (stable — customers quote these to us):
#   VM-100  unexpected internal error
#   VM-200  extraction rejected by the evidence gate
#   VM-300  a network service could not be reached
#
# Expected, actionable errors (ConfigError, workspace sign-in/binding,
# rejected API keys) pass through unchanged: they carry no internals and the
# user needs their guidance.

_EXPECTED = {
    "vetromar.errors.ConfigError",
    "vetromar.workspace.client.WorkspaceError",
    "vetromar.operations.InvalidApiKey",
}
_GATE = "vetromar.extraction.validate.ExtractionGateError"

INTERNAL_ERROR_MESSAGE = "Something went wrong. Please try again. (error VM-100)"


def _mro_names(exc: BaseException) -> set[str]:
    # Matching by qualified name keeps this module import-light — no pulling
    # extraction/workspace/operations into every importer of errors.py.
    return {f"{c.__module__}.{c.__qualname__}" for c in type(exc).__mro__}


def _is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    names = _mro_names(exc)
    return "httpx.HTTPError" in names or "urllib.error.URLError" in names


def present_error(exc: BaseException) -> tuple[str, str | None]:
    """What the user should see for `exc`: ``(message, code)``.

    ``code`` is None when the exception's own message is user-appropriate and
    passes through; otherwise a stable VM-* code identifying the failure
    class, with the real details left to the log."""
    names = _mro_names(exc)
    if names & _EXPECTED:
        return str(exc) or exc.__class__.__name__, None
    if _GATE in names:
        return (
            "Extraction couldn't be verified against the source, so nothing "
            "was saved. Please try again. (error VM-200)",
            "VM-200",
        )
    if _is_network_error(exc):
        return (
            "Couldn't reach the service — check your connection and try "
            "again. (error VM-300)",
            "VM-300",
        )
    return INTERNAL_ERROR_MESSAGE, "VM-100"
