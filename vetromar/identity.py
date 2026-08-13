"""The user's identity: one Ed25519 keypair, generated locally, never leaving
this machine except as its public half.

There are no accounts and no passwords anywhere in Vetromar — a graph host
knows a member by their public key. Enrollment (accepting an invite) and
sign-in (the challenge flow) both prove possession of the private key by
signing a server-issued nonce.

The private key lives at `~/.vetromar/identity.key` (PEM, 0600 — the same
secret-file idiom as every credential). Env-overridable so isolated
instances on one machine each get their own identity.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vetromar.config import VETROMAR_HOME


def identity_key_path() -> Path:
    return Path(os.environ.get("VETROMAR_IDENTITY_KEY", str(VETROMAR_HOME / "identity.key")))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Identity:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key

    @property
    def public_key(self) -> str:
        """The raw 32-byte Ed25519 public key, urlsafe-base64 (no padding) —
        the string a host stores and every wire payload carries."""
        raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return _b64(raw)

    def sign(self, message: str) -> str:
        """Sign a UTF-8 message (a challenge nonce), urlsafe-base64 signature."""
        return _b64(self._key.sign(message.encode()))


def load_identity() -> Identity | None:
    path = identity_key_path()
    if not path.exists():
        return None
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        return None
    return Identity(key)


def ensure_identity() -> Identity:
    """Load the identity, generating one on first use."""
    existing = load_identity()
    if existing is not None:
        return existing
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = identity_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    path.chmod(0o600)
    return Identity(key)
