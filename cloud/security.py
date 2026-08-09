"""Password hashing, token generation, and a minimal login rate limit."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()  # argon2id, library defaults

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed hash — treat as a failed login, never a 500.
        return False


def generate_token() -> str:
    """256-bit opaque token; only its SHA-256 is ever stored."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class RateLimiter:
    """In-memory per-key sliding-window counter for the credential routes.

    Deliberately minimal: argon2's cost already throttles offline attacks,
    and the real per-IP protection belongs to the reverse proxy at deploy
    time (see README). This just blunts naive online guessing in dev/v0.
    """

    def __init__(self, max_calls: int = 20, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.max_calls:
            return False
        hits.append(now)
        return True
