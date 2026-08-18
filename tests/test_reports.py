from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from work_history.api import create_app
from work_history.models import (
    ActivityEvent,
    Artifact,
    IngestDevice,
    SourceIdentity,
    SyncCursor,
)
from work_history.reports import _select_report_events, source_snapshot
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
        for source in ("jira", "confluence", "gitlab", "slack"):
            session.add(
                SyncCursor(
                    source=source,
                    stream=(
                        "work-mac"
                        if source == "gitlab"
                        else ("coverage" if source == "slack" else "default")
                    ),
                    cursor={"until": "2026-04-03T00:00:00+00:00"},
                )
            )
        session.commit()


def test_slack_socket_cursor_does_not_claim_contiguous_report_coverage(
    session_factory,
) -> None:
    period_end = datetime(2026, 4, 2, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            SyncCursor(
                source="slack",
                stream="socket",
                cursor={"until": period_end.isoformat()},
            )
        )
        session.commit()
        assert source_snapshot(session, period_end)["slack"]["status"] == "missing"


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


def test_report_context_selects_only_relevant_slack_collaboration(
    settings, session_factory
) -> None:
    private = _device(session_factory, "report-mac", "report_agent")
    start = datetime(2026, 3, 31, 16, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            SourceIdentity(
                source="slack",
                remote_id="U_SELF",
                display_name="본인",
                is_self=True,
            )
        )

        def artifact(remote_id: str, body: str) -> Artifact:
            model = Artifact(
                source="slack",
                remote_id=remote_id,
                kind="slack_message",
                title=remote_id,
                body_text=body,
            )
            session.add(model)
            session.flush()
            return model

        self_reply = artifact("T:C1:101.000", "제가 처리하겠습니다")
        thread_root = artifact("T:C1:100.000", "이 작업을 확인해 주세요")
        mention = artifact("T:C2:200.000", "<@U_SELF> 배포 검토 부탁드립니다")
        direct_message = artifact("T:D1:300.000", "진행 상황을 알려주세요")
        unrelated = artifact("T:C3:400.000", "다른 팀의 일반 대화")

        events = [
            ActivityEvent(
                source="jira",
                event_key="jira:1",
                kind="issue",
                action="changed",
                occurred_at=start,
                actor_is_self=True,
                title="Jira 작업",
                changes={},
            ),
            ActivityEvent(
                source="slack",
                event_key="slack:self-reply",
                kind="slack_thread_reply",
                action="thread_replied",
                occurred_at=start.replace(minute=1),
                actor_remote_id="U_SELF",
                actor_is_self=True,
                artifact=self_reply,
                artifact_remote_id=self_reply.remote_id,
                title="본인 답글",
                changes={"channel_id": "C1", "thread_ts": "100.000"},
            ),
            ActivityEvent(
                source="slack",
                event_key="slack:thread-root",
                kind="slack_message",
                action="message_posted",
                occurred_at=start.replace(minute=2),
                actor_remote_id="U_OTHER",
                actor_is_self=False,
                artifact=thread_root,
                artifact_remote_id=thread_root.remote_id,
                title="참여 스레드",
                changes={"channel_id": "C1", "thread_ts": None},
            ),
            ActivityEvent(
                source="slack",
                event_key="slack:mention",
                kind="slack_message",
                action="message_posted",
                occurred_at=start.replace(minute=3),
                actor_remote_id="U_OTHER",
                actor_is_self=False,
                artifact=mention,
                artifact_remote_id=mention.remote_id,
                title="본인 멘션",
                changes={"channel_id": "C2", "conversation_type": "public_channel"},
            ),
            ActivityEvent(
                source="slack",
                event_key="slack:reaction",
                kind="slack_reaction",
                action="reaction_added",
                occurred_at=start.replace(minute=4),
                actor_remote_id="U_OTHER",
                actor_is_self=False,
                artifact=self_reply,
                artifact_remote_id=self_reply.remote_id,
                title="본인 메시지 반응",
                changes={"channel_id": "C1", "reaction": "white_check_mark"},
            ),
            ActivityEvent(
                source="slack",
                event_key="slack:dm",
                kind="slack_message",
                action="message_posted",
                occurred_at=start.replace(minute=5),
                actor_remote_id="U_OTHER",
                actor_is_self=False,
                artifact=direct_message,
                artifact_remote_id=direct_message.remote_id,
                title="DM",
                changes={"channel_id": "D1", "conversation_type": "im"},
            ),
            ActivityEvent(
                source="slack",
                event_key="slack:unrelated",
                kind="slack_message",
                action="message_posted",
                occurred_at=start.replace(minute=6),
                actor_remote_id="U_OTHER",
                actor_is_self=False,
                artifact=unrelated,
                artifact_remote_id=unrelated.remote_id,
                title="무관한 대화",
                changes={"channel_id": "C3", "conversation_type": "public_channel"},
            ),
        ]
        session.add_all(events)
        for source in ("jira", "confluence", "gitlab", "slack"):
            session.add(
                SyncCursor(
                    source=source,
                    stream=(
                        "work-mac"
                        if source == "gitlab"
                        else ("coverage" if source == "slack" else "default")
                    ),
                    cursor={"until": "2026-04-03T00:00:00+00:00"},
                )
            )
        session.commit()

    response = _request(
        TestClient(create_app(settings, session_factory)),
        private,
        "report-mac",
        "POST",
        "/v1/report-agent/context",
        {"cadence": "daily", "period": "2026-04-01"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["activity_count"] == 6
    assert result["source_total_event_counts"]["slack"] == 6
    assert result["source_event_counts"]["slack"] == 5
    assert result["omitted_activity_count"] == 1
    assert result["selection_applied"] is True
    assert result["truncated"] is False
    titles = [item["title"] for item in result["activities"]]
    assert "무관한 대화" not in titles
    assert titles == [
        "Jira 작업",
        "본인 답글",
        "참여 스레드",
        "본인 멘션",
        "본인 메시지 반응",
        "DM",
    ]


def test_report_event_limit_keeps_source_and_self_priority_in_time_order() -> None:
    base = datetime(2026, 4, 1, tzinfo=UTC)
    events = [
        ActivityEvent(
            id="jira",
            source="jira",
            event_key="jira",
            kind="issue",
            action="changed",
            occurred_at=base.replace(hour=3),
            actor_is_self=True,
            title="Jira",
            changes={},
        ),
        ActivityEvent(
            id="self",
            source="slack",
            event_key="self",
            kind="slack_message",
            action="message_posted",
            occurred_at=base.replace(hour=1),
            actor_is_self=True,
            artifact_remote_id="T:C:1",
            title="Self",
            changes={"channel_id": "C"},
        ),
        ActivityEvent(
            id="dm",
            source="slack",
            event_key="dm",
            kind="slack_message",
            action="message_posted",
            occurred_at=base.replace(hour=2),
            actor_is_self=False,
            artifact_remote_id="T:D:2",
            title="DM",
            changes={"channel_id": "D", "conversation_type": "im"},
        ),
    ]
    selected, truncated = _select_report_events(events, {}, set(), maximum=2)
    assert truncated is True
    assert [item.id for item in selected] == ["self", "jira"]


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


def test_overall_report_context_and_storage(settings, session_factory) -> None:
    private = _device(session_factory, "report-mac", "report_agent")
    _seed_activity(session_factory)
    client = TestClient(create_app(settings, session_factory))
    period = "2026-04-01_to_2026-04-02"

    context = _request(
        client,
        private,
        "report-mac",
        "POST",
        "/v1/report-agent/context",
        {"cadence": "overall", "period": period},
    )
    assert context.status_code == 200, context.text
    assert context.json()["period"] == period
    assert context.json()["period_start"] == "2026-04-01"
    assert context.json()["period_end"] == "2026-04-03"
    assert context.json()["activity_count"] == 1

    path = f"/v1/reports/overall/{period}/work_report"
    payload = {
        "status": "final",
        "title": "전체 업무 보고서",
        "markdown": "# 전체 업무 보고서\n\n종합 내용",
        "source_snapshot": context.json()["source_snapshot"],
        "source_event_counts": context.json()["source_event_counts"],
        "prompt_version": "work-history-report-v2",
        "generator_model": "gpt-5.6-sol",
    }
    stored = _request(client, private, "report-mac", "PUT", path, payload)
    assert stored.status_code == 200, stored.text
    assert stored.json()["cadence"] == "overall"
    assert stored.json()["period"] == period
    assert stored.json()["period_end"] == "2026-04-03"

    fetched = client.get(path, headers={"Authorization": "Bearer read-test-token"})
    assert fetched.status_code == 200
    assert fetched.json()["markdown"].endswith("종합 내용")


def test_weekly_report_context_and_storage(settings, session_factory) -> None:
    private = _device(session_factory, "report-mac", "report_agent")
    _seed_activity(session_factory)
    client = TestClient(create_app(settings, session_factory))
    period = "2026-W14"

    context = _request(
        client,
        private,
        "report-mac",
        "POST",
        "/v1/report-agent/context",
        {"cadence": "weekly", "period": period},
    )
    assert context.status_code == 200, context.text
    assert context.json()["period"] == period
    assert context.json()["period_start"] == "2026-03-30"
    assert context.json()["period_end"] == "2026-04-06"
    assert context.json()["activity_count"] == 1

    path = f"/v1/reports/weekly/{period}/work_report"
    payload = {
        "status": "final",
        "title": "2026년 14주차 업무 보고서",
        "markdown": "# 주간 업무 보고서\n\n주간 종합 내용",
        "source_snapshot": context.json()["source_snapshot"],
        "source_event_counts": context.json()["source_event_counts"],
        "prompt_version": "work-history-report-v2",
        "generator_model": "gpt-5.6-sol",
    }
    stored = _request(client, private, "report-mac", "PUT", path, payload)
    assert stored.status_code == 200, stored.text
    assert stored.json()["cadence"] == "weekly"
    assert stored.json()["period"] == period
    assert stored.json()["period_end"] == "2026-04-06"


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

    weekly = _request(
        client,
        private,
        "report-mac",
        "POST",
        missing_path,
        {
            "cadence": "weekly",
            "from": "2026-04-01",
            "to": "2026-04-19",
            "include_partial": True,
        },
    )
    assert weekly.status_code == 200
    assert [item["period"] for item in weekly.json()["items"]] == [
        "2026-W14",
        "2026-W15",
        "2026-W16",
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
