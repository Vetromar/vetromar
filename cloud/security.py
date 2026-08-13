"""Keypair signature verification, token generation, and a minimal rate limit.

No passwords anywhere: identity is an Ed25519 public key, and every proof
is a signature over a server-issued single-use nonce.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections import defaultdict, deque

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_signature(public_key_b64: str, message: str, signature_b64: str) -> bool:
    """Did the holder of this public key sign this message? Malformed input
    reads as a failed proof, never a 500."""
    try:
        key = Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64))
        key.verify(_unb64(signature_b64), message.encode())
        return True
    except Exception:  # noqa: BLE001 — any failure is "not proven"
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
