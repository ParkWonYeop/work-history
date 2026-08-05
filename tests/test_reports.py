from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from work_history.api import create_app
from work_history.models import ActivityEvent, Artifact, IngestDevice, SyncCursor
from work_history.security import b64url_encode, sign_request


def _device(session_factory, device_id: str, purpose: str) -> Ed25519PrivateKey:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    with session_factory() as session:
        session.add(
            IngestDevice(
                device_id=device_id,
                public_key_b64=b64url_encode(public),
                purpose=purpose,
            )
        )
        session.commit()
    return private


def _signed_headers(
    private: Ed25519PrivateKey,
    device_id: str,
    method: str,
    path: str,
    body: bytes,
) -> dict[str, str]:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    nonce = str(uuid.uuid4())
    return {
        "Content-Type": "application/json",
        "X-WorkHistory-Device": device_id,
        "X-WorkHistory-Timestamp": timestamp,
        "X-WorkHistory-Nonce": nonce,
        "X-WorkHistory-Signature": sign_request(
            private,
            method,
            path,
            timestamp,
            nonce,
            body,
        ),
    }


def _request(
    client: TestClient,
    private: Ed25519PrivateKey,
    device_id: str,
    method: str,
    path: str,
    payload: dict,
):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return client.request(
        method,
        path,
        content=body,
        headers=_signed_headers(private, device_id, method, path, body),
    )


def _seed_activity(session_factory) -> None:
    with session_factory() as session:
        artifact = Artifact(
            source="jira",
            remote_id="WORK-1",
            kind="issue",
            title="보고서 API 구현",
            body_text="password=do-not-leak Authorization: Bearer abc123",
            state="Done",
            url="https://example.atlassian.net/browse/WORK-1",
        )
        session.add(artifact)
        session.flush()
        session.add(
            ActivityEvent(
                source="jira",
                event_key="WORK-1:done",
                kind="issue",
                action="transitioned",
                occurred_at=datetime(2026, 3, 31, 16, 30, tzinfo=UTC),
                actor_is_self=True,
                artifact_id=artifact.id,
                artifact_remote_id="WORK-1",
                title="보고서 API 구현",
                changes={"access_token": "should-hide"},
                url=artifact.url,
            )
        )
        for source in ("jira", "confluence", "gitlab"):
            session.add(
                SyncCursor(
                    source=source,
                    stream="default" if source != "gitlab" else "work-mac",
                    cursor={"until": "2026-04-03T00:00:00+00:00"},
                )
            )
        session.commit()


def test_report_context_redacts_content_and_enforces_device_purpose(
    settings, session_factory
) -> None:
    report_private = _device(session_factory, "report-mac", "report_agent")
    gitlab_private = _device(session_factory, "gitlab-mac", "gitlab_ingest")
    _seed_activity(session_factory)
    client = TestClient(create_app(settings, session_factory))
    path = "/v1/report-agent/context"
    payload = {"cadence": "daily", "period": "2026-04-01"}

    forbidden = _request(client, gitlab_private, "gitlab-mac", "POST", path, payload)
    assert forbidden.status_code == 403

    response = _request(client, report_private, "report-mac", "POST", path, payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["activity_count"] == 1
    assert result["all_sources_fresh"] is True
    assert result["from_time"].startswith("2026-03-31T15:00:00")
    serialized = json.dumps(result, ensure_ascii=False)
    assert "do-not-leak" not in serialized
    assert "abc123" not in serialized
    assert "should-hide" not in serialized
    assert "[REDACTED]" in serialized
    assert result["redaction_count"] >= 3


def test_report_upsert_is_idempotent_and_keeps_revisions(settings, session_factory) -> None:
    private = _device(session_factory, "report-mac", "report_agent")
    client = TestClient(create_app(settings, session_factory))
    path = "/v1/reports/daily/2026-04-01/work_report"
    payload = {
        "status": "partial",
        "title": "2026-04-01 업무 보고서",
        "markdown": "# 보고서\n\n부분 데이터",
        "source_snapshot": {"gitlab": {"status": "stale"}},
        "source_event_counts": {"jira": 1, "confluence": 0, "gitlab": 0},
        "prompt_version": "test-v1",
        "generator_model": "test-model",
    }
    first = _request(client, private, "report-mac", "PUT", path, payload)
    assert first.status_code == 200, first.text
    assert first.json()["current_revision"] == 1

    duplicate = _request(client, private, "report-mac", "PUT", path, payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["current_revision"] == 1

    payload["status"] = "final"
    payload["markdown"] = "# 보고서\n\n확정 데이터"
    payload["source_snapshot"] = {"gitlab": {"status": "fresh"}}
    final = _request(client, private, "report-mac", "PUT", path, payload)
    assert final.status_code == 200, final.text
    assert final.json()["current_revision"] == 2
    assert final.json()["status"] == "final"
    assert [item["revision"] for item in final.json()["versions"]] == [1, 2]

    auth = {"Authorization": "Bearer read-test-token"}
    stored = client.get(path, headers=auth)
    assert stored.status_code == 200
    assert stored.json()["markdown"].endswith("확정 데이터")


def test_missing_reports_cover_empty_calendar_days_and_months(settings, session_factory) -> None:
    private = _device(session_factory, "report-mac", "report_agent")
    client = TestClient(create_app(settings, session_factory))
    missing_path = "/v1/report-agent/missing"

    daily = _request(
        client,
        private,
        "report-mac",
        "POST",
        missing_path,
        {
            "cadence": "daily",
            "from": "2026-04-01",
            "to": "2026-04-03",
            "include_partial": True,
        },
    )
    assert daily.status_code == 200
    assert [item["period"] for item in daily.json()["items"]] == [
        "2026-04-01",
        "2026-04-02",
        "2026-04-03",
    ]
    assert all(len(item["missing_kinds"]) == 2 for item in daily.json()["items"])

    monthly = _request(
        client,
        private,
        "report-mac",
        "POST",
        missing_path,
        {
            "cadence": "monthly",
            "from": "2026-04-01",
            "to": "2026-06-30",
            "include_partial": True,
        },
    )
    assert monthly.status_code == 200
    assert [item["period"] for item in monthly.json()["items"]] == [
        "2026-04",
        "2026-05",
        "2026-06",
    ]


def test_report_period_parser_rejects_noncanonical_path(settings, session_factory) -> None:
    private = _device(session_factory, "report-mac", "report_agent")
    client = TestClient(create_app(settings, session_factory))
    path = "/v1/reports/monthly/2026-04-01/feedback"
    payload = {
        "status": "final",
        "title": "월간 피드백",
        "markdown": "# 월간 피드백",
        "source_snapshot": {},
        "source_event_counts": {},
        "prompt_version": "test-v1",
        "generator_model": "test-model",
    }
    response = _request(client, private, "report-mac", "PUT", path, payload)
    assert response.status_code == 422


def test_report_list_filters_by_date(settings, session_factory) -> None:
    private = _device(session_factory, "report-mac", "report_agent")
    client = TestClient(create_app(settings, session_factory))
    payload = {
        "status": "final",
        "title": "보고서",
        "markdown": "# 보고서",
        "source_snapshot": {},
        "source_event_counts": {},
        "prompt_version": "test-v1",
        "generator_model": "test-model",
    }
    for period in ("2026-04-01", "2026-04-02"):
        path = f"/v1/reports/daily/{period}/work_report"
        assert _request(client, private, "report-mac", "PUT", path, payload).status_code == 200

    response = client.get(
        "/v1/reports",
        params={"from": date(2026, 4, 2).isoformat(), "to": "2026-04-02"},
        headers={"Authorization": "Bearer read-test-token"},
    )
    assert response.status_code == 200
    assert [item["period"] for item in response.json()["items"]] == ["2026-04-02"]
