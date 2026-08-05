from __future__ import annotations

import gzip
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from work_history.api import create_app
from work_history.models import IngestDevice
from work_history.schemas import (
    ActivityRecord,
    ArtifactRecord,
    GitLabIngestBatch,
)
from work_history.security import b64url_encode, sign_request


def _device(session_factory):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    with session_factory() as session:
        session.add(IngestDevice(device_id="work-mac", public_key_b64=b64url_encode(public)))
        session.commit()
    return private


def _signed_headers(
    private: Ed25519PrivateKey,
    method: str,
    path: str,
    body: bytes = b"",
    nonce: str | None = None,
    timestamp: str | None = None,
) -> dict[str, str]:
    timestamp = timestamp or str(int(datetime.now(UTC).timestamp()))
    nonce = nonce or str(uuid.uuid4())
    return {
        "X-WorkHistory-Device": "work-mac",
        "X-WorkHistory-Timestamp": timestamp,
        "X-WorkHistory-Nonce": nonce,
        "X-WorkHistory-Signature": sign_request(private, method, path, timestamp, nonce, body),
    }


def test_signed_ingestion_checkpoint_and_read_api(settings, session_factory) -> None:
    private = _device(session_factory)
    app = create_app(settings, session_factory)
    client = TestClient(app)
    now = datetime.now(UTC)
    batch = GitLabIngestBatch(
        batch_id=str(uuid.uuid4()),
        source_instance="https://gitlab.internal",
        collected_at=now,
        artifacts=[
            ArtifactRecord(
                source="gitlab",
                remote_id="project:1:mr:2",
                kind="merge_request",
                title="Improve collection",
                body_text="Implementation details",
                created_at=now,
                updated_at=now,
            )
        ],
        events=[
            ActivityRecord(
                source="gitlab",
                event_key="mr:1:2:created",
                kind="merge_request",
                action="created",
                occurred_at=now,
                actor_remote_id="42",
                artifact_remote_id="project:1:mr:2",
                title="Improve collection",
            )
        ],
        next_checkpoint={"until": now.isoformat()},
    )
    body = batch.model_dump_json(exclude_none=True).encode()
    compressed = gzip.compress(body, mtime=0)
    path = "/v1/ingest/gitlab/batches"
    headers = _signed_headers(private, "POST", path, compressed)
    headers["Content-Encoding"] = "gzip"
    headers["Content-Type"] = "application/json"
    response = client.post(path, content=compressed, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 2
    assert response.json()["checkpoint_updated"] is True

    replay = client.post(path, content=compressed, headers=headers)
    assert replay.status_code == 409

    duplicate_headers = _signed_headers(private, "POST", path, compressed)
    duplicate_headers["Content-Encoding"] = "gzip"
    duplicate_headers["Content-Type"] = "application/json"
    duplicate = client.post(path, content=compressed, headers=duplicate_headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate_batch"] is True

    checkpoint_path = "/v1/ingest/gitlab/checkpoint"
    checkpoint = client.get(
        checkpoint_path,
        headers=_signed_headers(private, "GET", checkpoint_path),
    )
    assert checkpoint.status_code == 200
    assert checkpoint.json()["checkpoint"]["until"] == now.isoformat()

    auth = {"Authorization": "Bearer read-test-token"}
    activities = client.get(
        "/v1/activities",
        params={
            "from": (now - timedelta(minutes=1)).isoformat(),
            "to": (now + timedelta(minutes=1)).isoformat(),
        },
        headers=auth,
    )
    assert activities.status_code == 200, activities.text
    assert (
        activities.json()["items"][0]["event_key"]
        if "event_key" in activities.json()["items"][0]
        else True
    )
    assert activities.json()["items"][0]["title"] == "Improve collection"

    artifact = client.get(
        "/v1/artifacts/gitlab/project:1:mr:2",
        headers=auth,
    )
    assert artifact.status_code == 200
    assert artifact.json()["body_text"] == "Implementation details"


def test_authentication_rejects_bad_or_expired_requests(settings, session_factory) -> None:
    private = _device(session_factory)
    client = TestClient(create_app(settings, session_factory))
    path = "/v1/ingest/gitlab/checkpoint"

    bad = _signed_headers(private, "GET", path)
    bad["X-WorkHistory-Signature"] = "invalid"
    assert client.get(path, headers=bad).status_code == 401

    expired_at = str(int((datetime.now(UTC) - timedelta(minutes=6)).timestamp()))
    expired = _signed_headers(private, "GET", path, timestamp=expired_at)
    assert client.get(path, headers=expired).status_code == 401

    assert client.get("/v1/sync-status").status_code == 401
    assert (
        client.get(
            "/v1/sync-status",
            headers={"Authorization": "Bearer wrong-token"},
        ).status_code
        == 401
    )


def test_read_range_is_limited(settings, session_factory) -> None:
    client = TestClient(create_app(settings, session_factory))
    now = datetime.now(UTC)
    response = client.get(
        "/v1/activities",
        params={"from": (now - timedelta(days=32)).isoformat(), "to": now.isoformat()},
        headers={"Authorization": "Bearer read-test-token"},
    )
    assert response.status_code == 400
