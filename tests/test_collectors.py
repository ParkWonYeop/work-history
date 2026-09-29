from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from work_history.collectors.common import ApiClient
from work_history.collectors.confluence import ConfluenceCollector
from work_history.collectors.gitlab import GitLabCollector
from work_history.collectors.jira import JiraCollector
from work_history.collectors.slack import SlackCollector, message_text

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


def test_slack_collector_keeps_only_joined_conversations_and_all_authors() -> None:
    root_ts = f"{NOW.timestamp():.6f}"
    reply_ts = f"{(NOW + timedelta(minutes=1)).timestamp():.6f}"
    dm_ts = f"{(NOW + timedelta(minutes=2)).timestamp():.6f}"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode()
        if path == "/auth.test":
            return httpx.Response(200, json={"ok": True, "user_id": "U-ME", "team_id": "T1"})
        if path == "/users.list":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "members": [
                        {
                            "id": "U-ME",
                            "name": "me",
                            "profile": {"display_name": "Me"},
                        },
                        {
                            "id": "U-OTHER",
                            "name": "other",
                            "profile": {"display_name": "Other"},
                        },
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if path == "/users.conversations":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C-JOINED", "name": "team", "is_member": True},
                        {"id": "D1", "is_im": True, "user": "U-OTHER"},
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if path == "/conversations.history" and "channel=C-JOINED" in body:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": root_ts, "user": "U-OTHER", "text": "Team update", "reply_count": 1}
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if path == "/conversations.replies":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": root_ts, "user": "U-OTHER", "text": "Team update"},
                        {
                            "ts": reply_ts,
                            "thread_ts": root_ts,
                            "user": "U-ME",
                            "text": "My reply",
                        },
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if path == "/conversations.history" and "channel=D1" in body:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [{"ts": dm_ts, "user": "U-OTHER", "text": "Direct note"}],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        return httpx.Response(404, json={"ok": False, "error": "not_found"})

    collector = SlackCollector("https://workspace.example", "xoxp-test")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        batch = collector.collect(START, END)
    finally:
        collector.close()

    assert {artifact.body_text for artifact in batch.artifacts} == {
        "Team update",
        "My reply",
        "Direct note",
    }
    assert {event.action for event in batch.events} == {"message_posted", "thread_replied"}
    assert any(event.actor_is_self for event in batch.events)
    assert any(not event.actor_is_self for event in batch.events)
    assert {identity.remote_id for identity in batch.identities} == {"U-ME", "U-OTHER"}
    assert not any("C-OTHER" in artifact.remote_id for artifact in batch.artifacts)


def test_slack_socket_normalizes_edits_deletes_and_reactions() -> None:
    timestamp = f"{NOW.timestamp():.6f}"
    event_timestamp = f"{(NOW + timedelta(minutes=1)).timestamp():.6f}"
    collector = SlackCollector("https://workspace.example", "xoxp-test")
    collector.self_id = "U-ME"
    collector.team_id = "T1"
    collector.users = {
        "U-ME": {"id": "U-ME", "name": "me", "profile": {"display_name": "Me"}}
    }
    collector.conversations = {"C1": {"id": "C1", "name": "team", "is_member": True}}
    try:
        edited, _ = collector.normalize_socket_event(
            {
                "event_id": "EV-EDIT",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C1",
                    "event_ts": event_timestamp,
                    "previous_message": {"ts": timestamp, "user": "U-ME", "text": "Before"},
                    "message": {
                        "ts": timestamp,
                        "user": "U-ME",
                        "text": "After",
                        "edited": {"ts": event_timestamp, "user": "U-ME"},
                    },
                },
            }
        )
        deleted, _ = collector.normalize_socket_event(
            {
                "event_id": "EV-DELETE",
                "event": {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "C1",
                    "event_ts": event_timestamp,
                    "deleted_ts": timestamp,
                    "previous_message": {"ts": timestamp, "user": "U-ME", "text": "After"},
                },
            }
        )
        reacted, _ = collector.normalize_socket_event(
            {
                "event_id": "EV-REACTION",
                "event": {
                    "type": "reaction_added",
                    "event_ts": event_timestamp,
                    "user": "U-ME",
                    "reaction": "white_check_mark",
                    "item": {"type": "message", "channel": "C1", "ts": timestamp},
                },
            }
        )
    finally:
        collector.close()

    assert any(event.action == "message_edited" for event in edited.events)
    assert edited.artifacts[-1].body_text == "After"
    assert any(event.action == "message_deleted" for event in deleted.events)
    assert deleted.artifacts[-1].state == "deleted"
    assert any(event.action == "reaction_added" for event in reacted.events)


def test_slack_message_text_includes_blocks_attachments_and_files() -> None:
    assert message_text(
        {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Block"}}],
            "attachments": [{"text": "Attachment"}],
            "files": [{"title": "design.png"}],
        }
    ) == "Block\nAttachment\n[파일] design.png"


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
            assert "scope" not in request.url.params
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "author_id": 7,
                        "project_id": 1,
                        "action_name": "pushed to",
                        "target_type": None,
                        "created_at": NOW.isoformat(),
                        "push_data": {"commit_title": "Implement collector"},
                    },
                    {
                        "id": 2,
                        "author_id": 8,
                        "author": {"id": 8, "name": "Another User"},
                        "project_id": 1,
                        "action_name": "approved",
                        "target_type": "MergeRequest",
                        "target_iid": 2,
                        "target_title": "Improve collector",
                        "created_at": NOW.isoformat(),
                    },
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
    own_event = next(event for event in batch.events if event.event_key == "event:1")
    assert own_event.actor_remote_id == "7"
    assert own_event.actor_is_self
    foreign_event = next(event for event in batch.events if event.event_key == "event:2")
    assert foreign_event.actor_remote_id == "8"
    assert not foreign_event.actor_is_self


def test_jira_candidate_jql_uses_account_timezone() -> None:
    """JQL datetime literals have no offset, so they must be rendered in the
    account's timezone. Emitting UTC shifted the window and dropped recent issues."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(
                200,
                json={"accountId": "me", "displayName": "Me", "timeZone": "Asia/Seoul"},
            )
        if path == "/rest/api/3/search/jql":
            import json as _json

            captured.append(_json.loads(request.content)["jql"])
            return httpx.Response(200, json={"issues": [], "isLast": True})
        return httpx.Response(404)

    collector = JiraCollector("https://service.example", "me@example.com", "token")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        # 23:00Z -> 08:00 KST (next day), 02:00Z -> 11:00 KST
        collector.collect(
            datetime(2026, 8, 4, 23, 0, tzinfo=UTC),
            datetime(2026, 8, 5, 2, 0, tzinfo=UTC),
        )
    finally:
        collector.close()

    joined = " | ".join(captured)
    assert "2026-08-05 08:00" in joined, joined
    assert "2026-08-05 11:00" in joined, joined
    # the raw UTC rendering must not leak into the query
    assert "2026-08-04 23:00" not in joined, joined
    assert "2026-08-05 02:00" not in joined, joined


def test_confluence_cql_dates_are_padded_around_the_window() -> None:
    """CQL dates have no time and resolve in the user's timezone, so UTC dates alone
    clipped same-day edits; in_window() still enforces the exact bounds."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/wiki/rest/api/user/current":
            return httpx.Response(200, json={"accountId": "me"})
        if path == "/wiki/rest/api/content/search":
            captured.append(request.url.params["cql"])
            return httpx.Response(200, json={"results": [], "start": 0, "size": 0, "_links": {}})
        return httpx.Response(404)

    collector = ConfluenceCollector("https://service.example", "me@example.com", "token")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        # 08:00-11:00 KST on 2026-08-05, which is still 2026-08-04 in UTC at the start
        collector.collect(
            datetime(2026, 8, 4, 23, 0, tzinfo=UTC),
            datetime(2026, 8, 5, 2, 0, tzinfo=UTC),
        )
    finally:
        collector.close()
    assert len(captured) == 2
    for cql in captured:
        assert 'lastmodified >= "2026-08-03"' in cql, cql
        assert 'lastmodified <= "2026-08-06"' in cql, cql


def test_gitlab_event_dates_are_padded_because_after_and_before_are_exclusive() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/user":
            return httpx.Response(200, json={"id": 7, "username": "me"})
        if path == "/api/v4/users/7/events":
            captured.update(request.url.params)
        return httpx.Response(200, json=[])

    collector = GitLabCollector("https://service.example", "token")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        collector.collect(
            datetime(2026, 8, 7, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 14, 15, 0, tzinfo=UTC),
        )
    finally:
        collector.close()
    # after=2026-08-07 would have skipped 2026-08-07T15:00Z..2026-08-08T00:00Z
    assert captured["after"] == "2026-08-05"
    assert captured["before"] == "2026-08-16"


def test_slack_reconcile_reads_new_replies_on_older_threads() -> None:
    old_parent_ts = f"{(START - timedelta(days=3)).timestamp():.6f}"
    quiet_parent_ts = f"{(START - timedelta(days=4)).timestamp():.6f}"
    reply_ts = f"{NOW.timestamp():.6f}"
    history_oldest: list[float] = []
    replies_for: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(httpx.QueryParams(request.content.decode()))
        if path == "/auth.test":
            return httpx.Response(200, json={"ok": True, "user_id": "U-ME", "team_id": "T1"})
        if path == "/users.list":
            return httpx.Response(200, json={"ok": True, "members": [{"id": "U-ME"}]})
        if path == "/users.conversations":
            channels = [{"id": "C1", "name": "team"}]
            return httpx.Response(200, json={"ok": True, "channels": channels})
        if path == "/conversations.history":
            history_oldest.append(float(params["oldest"]))
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": old_parent_ts, "user": "U-ME", "text": "Old topic",
                         "reply_count": 3, "latest_reply": reply_ts},
                        {"ts": quiet_parent_ts, "user": "U-ME", "text": "Quiet topic",
                         "reply_count": 1, "latest_reply": quiet_parent_ts},
                    ],
                },
            )
        if path == "/conversations.replies":
            replies_for.append(params["ts"])
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"ts": old_parent_ts, "user": "U-ME", "text": "Old topic"},
                        {"ts": reply_ts, "thread_ts": old_parent_ts, "user": "U-ME",
                         "text": "Fresh reply"},
                    ],
                },
            )
        return httpx.Response(404, json={"ok": False, "error": "not_found"})

    collector = SlackCollector("https://workspace.example", "xoxp-test")
    collector.api.client.close()
    collector.api = _client(handler)
    try:
        batch = collector.collect(START, END)
    finally:
        collector.close()
    assert history_oldest and history_oldest[0] < (START - timedelta(days=4)).timestamp()
    assert replies_for == [old_parent_ts]
    assert {artifact.body_text for artifact in batch.artifacts} == {"Fresh reply"}
    assert [event.action for event in batch.events] == ["thread_replied"]


def test_jira_skips_issues_unchanged_since_a_covering_run_and_counts_failed_queries() -> None:
    requested: list[str] = []
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        path = request.url.path
        requested.append(path)
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me", "timeZone": "UTC"})
        if path == "/rest/api/3/search/jql":
            search_calls += 1
            if search_calls == 3:
                return httpx.Response(400, json={"errorMessages": ["bad worklog query"]})
            return httpx.Response(
                200,
                json={
                    "isLast": True,
                    "issues": [
                        {"key": "OLD-1", "fields": {"updated": "2026-08-05T00:30:00.000+0000"}},
                        {"key": "NEW-2", "fields": {"updated": "2026-08-05T00:40:00.000+0000"}},
                        {"key": "LATE-3", "fields": {"updated": "2026-08-05T09:00:00.000+0000"}},
                    ],
                },
            )
        if path.startswith("/rest/api/3/issue/"):
            if path.endswith("/changelog"):
                return httpx.Response(200, json={"values": [], "total": 0})
            if path.endswith("/comment"):
                return httpx.Response(200, json={"comments": [], "total": 0})
            if path.endswith("/worklog"):
                return httpx.Response(200, json={"worklogs": [], "total": 0})
            return httpx.Response(200, json={"fields": {"summary": "Issue"}})
        return httpx.Response(404)

    collector = JiraCollector("https://service.example", "me@example.com", "token")
    collector.api.client.close()
    collector.api = _client(handler)
    collector.known_updates = {
        "OLD-1": "2026-08-05T00:30:00.000+0000",
        "NEW-2": "2026-08-04T00:00:00.000+0000",
    }
    try:
        collector.collect(START, END)
    finally:
        collector.close()
    assert "/rest/api/3/issue/OLD-1" not in requested
    assert "/rest/api/3/issue/NEW-2" in requested
    assert collector.counters == {"failed_queries": 1, "skipped_unchanged": 1}
    # LATE-3 changed after this window ended, so a later run must still read it in full.
    assert collector.seen_updates == {
        "OLD-1": "2026-08-05T00:30:00.000+0000",
        "NEW-2": "2026-08-05T00:40:00.000+0000",
    }
