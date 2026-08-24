from __future__ import annotations

import logging
from datetime import UTC, datetime, tzinfo
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from work_history.collectors.common import (
    ApiClient,
    adf_to_text,
    in_window,
    parse_datetime,
)
from work_history.schemas import (
    ActivityRecord,
    ArtifactRecord,
    ArtifactUnavailableRecord,
    ArtifactVersionRecord,
    IdentityRecord,
    NormalizedBatch,
    RawRecordInput,
)

logger = logging.getLogger(__name__)


def _jql_timezone(name: Any) -> tzinfo:
    """Jira resolves bare JQL datetime literals in the account's timezone."""
    if not name:
        return UTC
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown Jira account timezone %r; falling back to UTC", name)
        return UTC


class JiraCollector:
    def __init__(self, site_url: str, email: str, api_token: str) -> None:
        client = httpx.Client(
            base_url=site_url,
            auth=(email, api_token),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self.site_url = site_url.rstrip("/")
        self.api = ApiClient(client)

    def close(self) -> None:
        self.api.client.close()

    def collect(self, start: datetime, end: datetime) -> NormalizedBatch:
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        myself = self.api.get_json("/rest/api/3/myself")
        account_id = str(myself["accountId"])
        batch = NormalizedBatch(
            identities=[
                IdentityRecord(
                    source="jira",
                    remote_id=account_id,
                    username=myself.get("emailAddress"),
                    display_name=myself.get("displayName"),
                    email=myself.get("emailAddress"),
                    raw=myself,
                )
            ]
        )
        keys = self._candidate_issue_keys(
            account_id, start, end, _jql_timezone(myself.get("timeZone"))
        )
        collected_at = datetime.now(UTC)
        for key in sorted(keys):
            try:
                self._collect_issue(batch, key, account_id, start, end, collected_at)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {403, 404}:
                    logger.warning("Jira issue became unavailable: %s", key)
                    batch.unavailable_artifacts.append(
                        ArtifactUnavailableRecord(
                            source="jira",
                            remote_id=key,
                            observed_at=collected_at,
                        )
                    )
                    continue
                raise
        return batch

    def _candidate_issue_keys(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
        jql_tz: tzinfo,
    ) -> set[str]:
        # JQL datetime literals carry no offset, so Jira reads them in the account's
        # timezone. Emitting UTC values here shifts the whole window and silently drops
        # the most recent hours of activity.
        local_start = start.astimezone(jql_tz)
        local_end = end.astimezone(jql_tz)
        start_day = local_start.strftime("%Y-%m-%d")
        end_day = local_end.strftime("%Y-%m-%d")
        start_minute = local_start.strftime("%Y-%m-%d %H:%M")
        end_minute = local_end.strftime("%Y-%m-%d %H:%M")
        queries = [
            (
                f'issuekey in updatedBy("{account_id}", "{start_day}", "{end_day}") '
                f'AND updated < "{end_minute}"'
            ),
            (
                "(assignee=currentUser() OR reporter=currentUser() OR creator=currentUser()) "
                f'AND updated >= "{start_minute}" AND updated < "{end_minute}"'
            ),
            (
                "worklogAuthor=currentUser() "
                f'AND worklogDate >= "{start_day}" AND worklogDate <= "{end_day}"'
            ),
        ]
        keys: set[str] = set()
        successful_queries = 0
        for jql in queries:
            token: str | None = None
            try:
                while True:
                    body: dict[str, Any] = {
                        "jql": jql,
                        "fields": ["key"],
                        "maxResults": 100,
                    }
                    if token:
                        body["nextPageToken"] = token
                    payload = self.api.post_json("/rest/api/3/search/jql", json=body)
                    keys.update(str(issue["key"]) for issue in payload.get("issues", []))
                    token = payload.get("nextPageToken")
                    if not token or payload.get("isLast") is True:
                        break
                successful_queries += 1
            except httpx.HTTPStatusError as exc:
                logger.warning("Jira candidate JQL failed status=%s", exc.response.status_code)
        if not successful_queries:
            raise RuntimeError("all Jira candidate searches failed")
        return keys

    def _collect_issue(
        self,
        batch: NormalizedBatch,
        key: str,
        account_id: str,
        start: datetime,
        end: datetime,
        collected_at: datetime,
    ) -> None:
        fields = [
            "summary",
            "description",
            "status",
            "assignee",
            "reporter",
            "creator",
            "project",
            "issuetype",
            "created",
            "updated",
            "parent",
            "labels",
        ]
        issue = self.api.get_json(
            f"/rest/api/3/issue/{quote(key)}",
            params={"fields": ",".join(fields)},
        )
        issue_fields = issue.get("fields", {})
        url = f"{self.site_url}/browse/{key}"
        batch.artifacts.append(
            ArtifactRecord(
                source="jira",
                remote_id=key,
                kind=issue_fields.get("issuetype", {}).get("name", "issue"),
                title=issue_fields.get("summary") or key,
                body_text=adf_to_text(issue_fields.get("description")).strip(),
                state=issue_fields.get("status", {}).get("name"),
                namespace=issue_fields.get("project", {}).get("key"),
                url=url,
                created_at=parse_datetime(issue_fields.get("created")),
                updated_at=parse_datetime(issue_fields.get("updated")),
                raw=issue,
            )
        )
        batch.raw_records.append(
            RawRecordInput(
                source="jira",
                record_key=f"issue:{key}:{issue_fields.get('updated')}",
                kind="issue",
                payload=issue,
                collected_at=collected_at,
            )
        )
        creator_id = (issue_fields.get("creator") or {}).get("accountId")
        created_at = parse_datetime(issue_fields.get("created"))
        if creator_id == account_id and in_window(created_at, start, end):
            batch.events.append(
                ActivityRecord(
                    source="jira",
                    event_key=f"issue:{key}:created",
                    kind="issue",
                    action="created",
                    occurred_at=created_at,
                    actor_remote_id=account_id,
                    artifact_remote_id=key,
                    title=issue_fields.get("summary") or key,
                    url=url,
                )
            )
        self._collect_changelog(batch, key, account_id, start, end, url)
        self._collect_comments(batch, key, account_id, start, end, url, collected_at)
        self._collect_worklogs(batch, key, account_id, start, end, url, collected_at)

    def _collect_changelog(
        self,
        batch: NormalizedBatch,
        key: str,
        account_id: str,
        start: datetime,
        end: datetime,
        url: str,
    ) -> None:
        start_at = 0
        while True:
            payload = self.api.get_json(
                f"/rest/api/3/issue/{quote(key)}/changelog",
                params={"startAt": start_at, "maxResults": 100},
            )
            values = payload.get("values", [])
            for history in values:
                occurred_at = parse_datetime(history.get("created"))
                if not in_window(occurred_at, start, end):
                    continue
                actor_id = (history.get("author") or {}).get("accountId")
                relevant_assignment = any(
                    item.get("field") == "assignee"
                    and account_id in {item.get("from"), item.get("to")}
                    for item in history.get("items", [])
                )
                if actor_id != account_id and not relevant_assignment:
                    continue
                changes = {
                    item.get("field") or item.get("fieldId") or "field": {
                        "from": item.get("fromString"),
                        "to": item.get("toString"),
                    }
                    for item in history.get("items", [])
                }
                batch.events.append(
                    ActivityRecord(
                        source="jira",
                        event_key=f"issue:{key}:changelog:{history['id']}",
                        kind="issue_change",
                        action="changed" if actor_id == account_id else "assigned",
                        occurred_at=occurred_at,
                        actor_remote_id=actor_id,
                        actor_is_self=actor_id == account_id,
                        artifact_remote_id=key,
                        title=key,
                        changes=changes,
                        url=url,
                        raw=history,
                    )
                )
            start_at += len(values)
            if not values or start_at >= payload.get("total", start_at):
                break

    def _collect_comments(
        self,
        batch: NormalizedBatch,
        key: str,
        account_id: str,
        start: datetime,
        end: datetime,
        url: str,
        collected_at: datetime,
    ) -> None:
        start_at = 0
        while True:
            payload = self.api.get_json(
                f"/rest/api/3/issue/{quote(key)}/comment",
                params={"startAt": start_at, "maxResults": 100, "orderBy": "created"},
            )
            comments = payload.get("comments", [])
            for comment in comments:
                comment_id = str(comment["id"])
                author_id = (comment.get("author") or {}).get("accountId")
                created_at = parse_datetime(comment.get("created"))
                updated_at = parse_datetime(comment.get("updated"))
                body = adf_to_text(comment.get("body")).strip()
                batch.versions.append(
                    ArtifactVersionRecord(
                        source="jira",
                        artifact_remote_id=key,
                        remote_version_id=f"comment:{comment_id}:{comment.get('updated')}",
                        author_remote_id=author_id,
                        body_text=body,
                        created_at=updated_at or created_at,
                        raw=comment,
                    )
                )
                if author_id == account_id and in_window(updated_at or created_at, start, end):
                    action = "commented" if updated_at == created_at else "comment_edited"
                    batch.events.append(
                        ActivityRecord(
                            source="jira",
                            event_key=f"issue:{key}:comment:{comment_id}:{comment.get('updated')}",
                            kind="comment",
                            action=action,
                            occurred_at=updated_at or created_at,
                            actor_remote_id=account_id,
                            artifact_remote_id=key,
                            title=key,
                            changes={"body": body},
                            url=f"{url}?focusedCommentId={comment_id}",
                        )
                    )
                batch.raw_records.append(
                    RawRecordInput(
                        source="jira",
                        record_key=f"comment:{comment_id}:{comment.get('updated')}",
                        kind="comment",
                        payload=comment,
                        collected_at=collected_at,
                    )
                )
            start_at += len(comments)
            if not comments or start_at >= payload.get("total", start_at):
                break

    def _collect_worklogs(
        self,
        batch: NormalizedBatch,
        key: str,
        account_id: str,
        start: datetime,
        end: datetime,
        url: str,
        collected_at: datetime,
    ) -> None:
        start_at = 0
        while True:
            payload = self.api.get_json(
                f"/rest/api/3/issue/{quote(key)}/worklog",
                params={"startAt": start_at, "maxResults": 100},
            )
            worklogs = payload.get("worklogs", [])
            for worklog in worklogs:
                author_id = (worklog.get("author") or {}).get("accountId")
                started_at = parse_datetime(worklog.get("started"))
                if author_id != account_id or not in_window(started_at, start, end):
                    continue
                worklog_id = str(worklog["id"])
                comment = adf_to_text(worklog.get("comment")).strip()
                batch.events.append(
                    ActivityRecord(
                        source="jira",
                        event_key=f"issue:{key}:worklog:{worklog_id}:{worklog.get('updated')}",
                        kind="worklog",
                        action="logged_work",
                        occurred_at=started_at,
                        actor_remote_id=account_id,
                        artifact_remote_id=key,
                        title=key,
                        changes={
                            "time_spent_seconds": worklog.get("timeSpentSeconds"),
                            "comment": comment,
                        },
                        url=url,
                        raw=worklog,
                    )
                )
                batch.raw_records.append(
                    RawRecordInput(
                        source="jira",
                        record_key=f"worklog:{worklog_id}:{worklog.get('updated')}",
                        kind="worklog",
                        payload=worklog,
                        collected_at=collected_at,
                    )
                )
            start_at += len(worklogs)
            if not worklogs or start_at >= payload.get("total", start_at):
                break
