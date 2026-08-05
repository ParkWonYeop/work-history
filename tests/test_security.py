from __future__ import annotations

from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from work_history.security import (
    b64url_encode,
    sign_request,
    timestamp_is_fresh,
    verify_request_signature,
)


def test_ed25519_request_signature_covers_body_and_path() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    timestamp = str(int(datetime.now(UTC).timestamp()))
    signature = sign_request(private, "POST", "/v1/test", timestamp, "nonce", b"body")

    assert verify_request_signature(
        b64url_encode(public),
        signature,
        "POST",
        "/v1/test",
        timestamp,
        "nonce",
        b"body",
    )
    assert not verify_request_signature(
        b64url_encode(public),
        signature,
        "POST",
        "/v1/other",
        timestamp,
        "nonce",
        b"body",
    )
    assert not verify_request_signature(
        b64url_encode(public),
        signature,
        "POST",
        "/v1/test",
        timestamp,
        "nonce",
        b"changed",
    )


def test_timestamp_freshness() -> None:
    now = int(datetime.now(UTC).timestamp())
    assert timestamp_is_fresh(str(now), 300)
    assert not timestamp_is_fresh(str(now - 301), 300)
    assert not timestamp_is_fresh("not-a-time", 300)
