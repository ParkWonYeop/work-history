from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def signing_message(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    return (f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{sha256_hex(body)}").encode()


def sign_request(
    private_key: Ed25519PrivateKey,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    return b64url_encode(private_key.sign(signing_message(method, path, timestamp, nonce, body)))


def verify_request_signature(
    public_key_b64: str,
    signature_b64: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(b64url_decode(public_key_b64))
        key.verify(
            b64url_decode(signature_b64),
            signing_message(method, path, timestamp, nonce, body),
        )
        return True
    except (ValueError, InvalidSignature):
        return False


def timestamp_is_fresh(timestamp: str, max_age_seconds: int) -> bool:
    try:
        sent_at = datetime.fromtimestamp(int(timestamp), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return False
    age = abs((datetime.now(UTC) - sent_at).total_seconds())
    return age <= max_age_seconds


def bearer_matches(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided, expected)
