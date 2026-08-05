from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from work_history.collectors.common import ApiClient
from work_history.collectors.confluence import ConfluenceCollector
from work_history.collectors.gitlab import GitLabCollector
from work_history.collectors.jira import JiraCollector

NOW = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
START = NOW - timedelta(hours=2)
END = NOW + timedelta(hours=1)


def _client(handler) -> ApiClient:
    return ApiClient(
        httpx.Client(
            base_url="https://service.example",
            transport=httpx.MockTransport(handler),
        )
    )


def test_jira_collector_normalizes_changes_comments_and_worklogs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me", "displayName": "Me"})
        if path == "/rest/api/3/search/jql":
            return httpx.Response(200, json={"issues": [{"key": "ABC-1"}], "isLast": True})
        if path == "/rest/api/3/issue/ABC-1":
            return httpx.Response(
                200,
                json={
                    "id": "1",
                    "key": "ABC-1",
                    "fields": {
                        "summary": "Build collector",
                        "description": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Details"}],
                                }
                            ],
                        },
                        "status": {"name": "Done"},
                        "creator": {"accountId": "me"},
                        "project": {"key": "ABC"},
                        "issuetype": {"name": "Task"},
                        "created": START.isoformat(),
                        "updated": NOW.isoformat(),
                    },
                },
            )
        if path.endswith("/changelog"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "id": "c1",
                            "created": NOW.isoformat(),
                            "author": {"accountId": "me"},
                            "items": [
                                {"field": "status", "fromString": "Doing", "toString": "Done"}
                            ],
                        }
                    ],
                    "total": 1,
                },
            )
        if path.endswith("/comment"):
            return httpx.Response(
                200,
                json={
                    "comments": [
                        {
                            "id": "10",
                            "author": {"accountId": "me"},
                            "created": NOW.isoformat(),
                            "updated": NOW.isoformat(),
                            "body": {
                                "type": "doc",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "Done"}],
                                    }
                                ],
                            },
                        }
                    ],
                    "total": 1,
                },
            )
        if path.endswith("/worklog"):
            return httpx.Response(
                200,
                json={
                    "worklogs": [
                        {
                            "id": "20",
                            "author": {"accountId": "me"},
                            "started": NOW.isoformat(),
                            "updated": NOW.isoformat(),
                            "timeSpentSeconds": 3600,
                        }
                    ],
                    "total": 1,
                },
            )
        return httpx.Response(404)

    collector = JiraCollector("https://service.example", "me@example.com", "token")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        batch = collector.collect(START, END)
    finally:
        collector.close()
    assert len(batch.artifacts) == 1
    assert {event.action for event in batch.events} >= {
        "created",
        "changed",
        "commented",
        "logged_work",
    }
    assert batch.artifacts[0].body_text == "Details"


def test_confluence_collector_normalizes_versions_and_comments() -> None:
    page = {
        "id": "10",
        "type": "page",
        "title": "Design",
        "status": "current",
        "body": {"storage": {"value": "<p>Current design</p>"}},
        "version": {"number": 2, "when": NOW.isoformat(), "by": {"accountId": "me"}},
        "history": {"createdDate": START.isoformat(), "createdBy": {"accountId": "me"}},
        "space": {"key": "ENG"},
        "_links": {"webui": "/spaces/ENG/pages/10"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/wiki/rest/api/user/current":
            return httpx.Response(200, json={"accountId": "me", "displayName": "Me"})
        if path == "/wiki/rest/api/content/search":
            cql = request.url.params.get("cql", "")
            return httpx.Response(
                200,
                json={
                    "results": [] if cql.startswith("type=comment") else [page],
                    "start": 0,
                    "size": 0 if cql.startswith("type=comment") else 1,
                    "_links": {},
                },
            )
        if path == "/wiki/api/v2/pages/10/versions":
            return httpx.Response(
                200,
                json={
                    "results": [{"number": 2, "authorId": "me", "createdAt": NOW.isoformat()}],
                    "_links": {},
                },
            )
        if path == "/wiki/api/v2/pages/10/versions/2":
            return httpx.Response(
                200, json={"title": "Design", "body": {"storage": {"value": "<p>Version two</p>"}}}
            )
        if path == "/wiki/rest/api/content/10/child/comment":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "30",
                            "body": {"storage": {"value": "<p>Looks good</p>"}},
                            "version": {"number": 1, "when": NOW.isoformat()},
                            "history": {
                                "createdDate": NOW.isoformat(),
                                "createdBy": {"accountId": "me"},
                            },
                        }
                    ],
                    "_links": {},
                },
            )
        return httpx.Response(404)

    collector = ConfluenceCollector("https://service.example", "me@example.com", "token")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        batch = collector.collect(START, END)
    finally:
        collector.close()
    assert batch.artifacts[0].body_text == "Current design"
    assert any(version.body_text == "Version two" for version in batch.versions)
    assert any(event.action == "commented" for event in batch.events)


def test_gitlab_collector_supplements_events_with_mrs_and_commits() -> None:
    mr = {
        "project_id": 1,
        "iid": 2,
        "title": "Improve collector",
        "description": "MR details",
        "state": "opened",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "author": {"id": 7},
        "references": {"full": "group/project!2"},
        "web_url": "https://service.example/group/project/-/merge_requests/2",
    }
    commit = {
        "id": "abc123",
        "title": "Implement collector",
        "message": "Implement collector",
        "author_name": "Me",
        "author_email": "me@example.com",
        "authored_date": NOW.isoformat(),
        "committed_date": NOW.isoformat(),
        "web_url": "https://service.example/group/project/-/commit/abc123",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/user":
            return httpx.Response(
                200, json={"id": 7, "username": "me", "name": "Me", "email": "me@example.com"}
            )
        if path == "/api/v4/user/emails":
            return httpx.Response(200, json=[{"email": "me@example.com"}])
        if path == "/api/v4/users/7/events":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "project_id": 1,
                        "action_name": "pushed to",
                        "target_type": None,
                        "created_at": NOW.isoformat(),
                        "push_data": {"commit_title": "Implement collector"},
                    }
                ],
            )
        if path == "/api/v4/merge_requests":
            return httpx.Response(200, json=[mr])
        if path.endswith("/discussions"):
            return httpx.Response(200, json=[])
        if path.endswith("/commits") and "/merge_requests/" in path:
            return httpx.Response(200, json=[commit])
        if path.endswith("/approvals"):
            return httpx.Response(200, json={"approved_by": []})
        if path == "/api/v4/issues":
            return httpx.Response(200, json=[])
        if path == "/api/v4/projects/1/repository/commits":
            return httpx.Response(200, json=[commit])
        return httpx.Response(404)

    collector = GitLabCollector("https://service.example", "token")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        batch = collector.collect(START, END)
    finally:
        collector.close()
    assert any(artifact.kind == "merge_request" for artifact in batch.artifacts)
    assert any(artifact.kind == "commit" for artifact in batch.artifacts)
    assert any(event.action == "committed" for event in batch.events)
    assert any(event.event_key == "event:1" for event in batch.events)
