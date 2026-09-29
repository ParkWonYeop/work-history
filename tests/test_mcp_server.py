from __future__ import annotations

import json
from pathlib import Path

import anyio
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

pytest.importorskip("mcp")

from mcp.client.client import Client  # noqa: E402

from work_history import mcp_server  # noqa: E402
from work_history.mcp_server import Backend, build_server  # noqa: E402
from work_history.report_agent import ReportServerClient  # noqa: E402

BASE_URL = "https://history.example"


def _event(event_id: str, title: str, *, own: bool, at: str) -> dict:
    return {
        "id": event_id,
        "source": "jira",
        "kind": "issue_change",
        "action": "changed",
        "occurred_at": at,
        "actor_remote_id": "me" if own else "other",
        "actor_is_self": own,
        "artifact_remote_id": "ABC-1",
        "title": title,
        "changes": {"status": {"from": "Open", "to": "Done"}},
        "url": f"{BASE_URL}/browse/ABC-1",
    }


CONTEXT = {
    "cadence": "daily",
    "period": "2026-09-28",
    "period_start": "2026-09-28",
    "period_end": "2026-09-29",
    "from_time": "2026-09-27T15:00:00+00:00",
    "to_time": "2026-09-28T15:00:00+00:00",
    "activity_count": 3,
    "source_event_counts": {"jira": 3, "confluence": 0, "gitlab": 0, "slack": 0},
    "source_total_event_counts": {"jira": 3, "confluence": 0, "gitlab": 0, "slack": 0},
    "omitted_activity_count": 0,
    "activities": [
        _event(str(index), f"ABC-1 step {index}", own=True, at="2026-09-28T01:00:00+00:00")
        for index in range(3)
    ],
    "artifacts": [
        {
            "source": "jira",
            "remote_id": "ABC-1",
            "kind": "Task",
            "title": "Fix sync",
            "body_text": "x" * 9_000,
            "state": "Done",
            "namespace": "ABC",
            "url": f"{BASE_URL}/browse/ABC-1",
            "created_at_remote": "2026-09-20T00:00:00+00:00",
            "updated_at_remote": "2026-09-28T01:00:00+00:00",
            "versions": [
                {
                    "remote_version_id": f"comment:{index}",
                    "author_remote_id": "me",
                    "body_text": "note",
                    "created_at_remote": "2026-09-28T01:00:00+00:00",
                }
                for index in range(25)
            ],
        }
    ],
    "daily_documents": [],
    "source_snapshot": {"jira": {"status": "fresh"}, "slack": {"status": "stale"}},
    "source_snapshot_hash": "hash",
    "all_sources_fresh": False,
    "redaction_count": 0,
    "truncated": False,
    "existing": [],
}


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/report-agent/context":
            return httpx.Response(200, json=CONTEXT)
        if path == "/v1/report-agent/missing":
            return httpx.Response(200, json={"cadence": "daily", "items": []})
        if request.method == "PUT" and path == "/v1/reports/daily/2026-09-28/work_report":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "cadence": "daily",
                    "period": "2026-09-28",
                    "kind": "work_report",
                    "status": body["status"],
                    "title": body["title"],
                    "current_revision": 2,
                    "content_sha256": "sha",
                    "updated_at": "2026-09-29T02:00:00+00:00",
                },
            )
        if path == "/v1/activities":
            return self._activities(request.url.params)
        if path.startswith("/v1/artifacts/"):
            return httpx.Response(404, json={"detail": "artifact not found"})
        return httpx.Response(404, json={"detail": "not found"})

    @staticmethod
    def _activities(params: httpx.QueryParams) -> httpx.Response:
        if params["from"].startswith("2026-09-01"):
            items = [_event("d", "WORK-1 deploy", own=True, at="2026-09-02T01:00:00+00:00")]
            return httpx.Response(200, json={"items": items, "next_cursor": None})
        if "cursor" not in params:
            items = [
                _event("a", "WORK-1 fix", own=True, at="2026-08-02T01:00:00+00:00"),
                _event("b", "WORK-1 chat", own=False, at="2026-08-02T02:00:00+00:00"),
            ]
            return httpx.Response(200, json={"items": items, "next_cursor": "page-2"})
        items = [_event("c", "work-1 review", own=True, at="2026-08-03T01:00:00+00:00")]
        return httpx.Response(200, json={"items": items, "next_cursor": None})


def _backend(api: FakeApi) -> Backend:
    transport = httpx.MockTransport(api)
    signed = ReportServerClient(BASE_URL, "codex-report-agent", Ed25519PrivateKey.generate())
    signed.client.close()
    signed.client = httpx.Client(base_url=BASE_URL, transport=transport)
    read = httpx.Client(
        base_url=BASE_URL,
        transport=transport,
        headers={"Authorization": "Bearer read-token"},
    )
    return Backend(Path("unused.toml"), signed=signed, read=read)


def _call(backend: Backend, *calls: tuple[str, dict]) -> list:
    async def run() -> list:
        async with Client(build_server(backend)) as client:
            return [await client.call_tool(name, arguments) for name, arguments in calls]

    return anyio.run(run)


def test_tools_are_listed_with_write_hints_only_on_put_report() -> None:
    async def run():
        async with Client(build_server(_backend(FakeApi()))) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(run)}
    assert set(tools) == {
        "get_report_context",
        "list_missing_reports",
        "search_activities",
        "get_artifact",
        "get_report",
        "put_report",
    }
    assert tools["put_report"].annotations.read_only_hint is False
    assert all(
        tool.annotations.read_only_hint for name, tool in tools.items() if name != "put_report"
    )


def test_report_context_is_summarized_paged_and_fetched_once() -> None:
    api = FakeApi()
    summary, first, second, artifacts = _call(
        _backend(api),
        ("get_report_context", {"cadence": "daily", "period": "2026-09-28"}),
        ("get_report_context", {"cadence": "daily", "period": "2026-09-28",
                                "section": "activities", "limit": 2}),
        ("get_report_context", {"cadence": "daily", "period": "2026-09-28",
                                "section": "activities", "offset": 2}),
        ("get_report_context", {"cadence": "daily", "period": "2026-09-28",
                                "section": "artifacts"}),
    )
    assert summary.structured_content["upload_status"] == "partial"
    assert summary.structured_content["from_time"] == "2026-09-28T00:00:00+09:00"
    assert summary.structured_content["sections"] == {
        "activities": 3,
        "artifacts": 1,
        "daily_documents": 0,
    }
    assert "activities" not in summary.structured_content
    assert len(first.structured_content["items"]) == 2
    assert first.structured_content["next_offset"] == 2
    assert first.structured_content["items"][0]["occurred_at"] == "2026-09-28T10:00:00+09:00"
    assert second.structured_content["next_offset"] is None
    artifact = artifacts.structured_content["items"][0]
    assert len(artifact["body_text"]) < 9_000 and "생략" in artifact["body_text"]
    assert len(artifact["versions"]) == 20
    assert artifact["omitted_versions"] == 5
    assert [request.url.path for request in api.requests].count("/v1/report-agent/context") == 1


def test_search_activities_splits_long_ranges_filters_and_pages() -> None:
    api = FakeApi()
    arguments = {
        "from_date": "2026-08-01",
        "to_date": "2026-09-09",
        "query": "WORK-1",
        "self_only": True,
        "limit": 2,
    }
    first, second = _call(
        _backend(api),
        ("search_activities", arguments),
        ("search_activities", {**arguments, "offset": 2}),
    )
    assert [item["title"] for item in first.structured_content["items"]] == [
        "WORK-1 fix",
        "work-1 review",
    ]
    assert first.structured_content["next_offset"] == 2
    assert [item["title"] for item in second.structured_content["items"]] == ["WORK-1 deploy"]
    assert second.structured_content["next_offset"] is None
    request = api.requests[0]
    assert request.headers["Authorization"] == "Bearer read-token"
    assert request.url.params["from"] == "2026-08-01T00:00:00+09:00"
    assert request.url.params["to"] == "2026-09-01T00:00:00+09:00"


def test_search_activities_rejects_ranges_over_the_limit() -> None:
    (result,) = _call(
        _backend(FakeApi()),
        ("search_activities", {"from_date": "2026-01-01", "to_date": "2026-06-01"}),
    )
    assert result.is_error
    assert "93 days" in result.content[0].text


def test_put_report_stores_the_snapshot_of_the_context_it_was_written_from() -> None:
    api = FakeApi()
    _, stored = _call(
        _backend(api),
        ("get_report_context", {"cadence": "daily", "period": "2026-09-28"}),
        (
            "put_report",
            {
                "cadence": "daily",
                "period": "2026-09-28",
                "kind": "work_report",
                "title": "2026-09-28 업무 보고서",
                "markdown": "# 2026-09-28 업무 보고서\n\n## 종합 정리\n\n내용",
                "generator_model": "claude-opus-5-5",
            },
        ),
    )
    assert not stored.is_error
    assert stored.structured_content["status"] == "partial"
    assert stored.structured_content["current_revision"] == 2
    put = next(request for request in api.requests if request.method == "PUT")
    body = json.loads(put.content)
    assert body["source_snapshot"] == CONTEXT["source_snapshot"]
    assert body["source_event_counts"] == CONTEXT["source_event_counts"]
    assert body["prompt_version"] == "work-history-mcp-v1"
    assert body["generator_model"] == "claude-opus-5-5"
    assert put.headers["X-WorkHistory-Signature"]
    assert [request.url.path for request in api.requests].count("/v1/report-agent/context") == 1


def test_api_errors_reach_the_client() -> None:
    (result,) = _call(
        _backend(FakeApi()),
        ("get_artifact", {"source": "jira", "remote_id": "MISSING-1"}),
    )
    assert result.is_error
    assert "404" in result.content[0].text
    assert "artifact not found" in result.content[0].text


def test_missing_read_token_points_to_set_read_token(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text('device_id = "codex-report-agent"\nserver_url = "https://history.example"\n')
    monkeypatch.delenv("WORK_HISTORY_READ_API_TOKEN", raising=False)
    monkeypatch.setattr(mcp_server.keyring, "get_password", lambda service, account: None)
    (result,) = _call(
        Backend(config),
        ("get_report", {"cadence": "daily", "period": "2026-09-28", "kind": "work_report"}),
    )
    assert result.is_error
    assert "set-read-token" in result.content[0].text
