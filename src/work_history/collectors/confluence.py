from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from work_history.collectors.common import ApiClient, html_to_text, in_window, parse_datetime
from work_history.schemas import (
    ActivityRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    IdentityRecord,
    NormalizedBatch,
    RawRecordInput,
)

logger = logging.getLogger(__name__)


class ConfluenceCollector:
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
        myself = self.api.get_json("/wiki/rest/api/user/current")
        account_id = str(myself["accountId"])
        batch = NormalizedBatch(
            identities=[
                IdentityRecord(
                    source="confluence",
                    remote_id=account_id,
                    username=myself.get("username"),
                    display_name=myself.get("displayName"),
                    email=myself.get("email"),
                    raw=myself,
                )
            ]
        )
        collected_at = datetime.now(UTC)
        seen: set[str] = set()
        # CQL date literals have no time or offset and resolve in the user's timezone, so
        # UTC dates clipped same-day edits. Widen by a day each way; in_window() below keeps
        # the exact [start, end) boundary.
        cql_from = (start - timedelta(days=1)).strftime("%Y-%m-%d")
        cql_to = (end + timedelta(days=1)).strftime("%Y-%m-%d")
        cql_queries = [
            (
                "(creator=currentUser() OR contributor=currentUser()) "
                f'AND lastmodified >= "{cql_from}" AND lastmodified <= "{cql_to}"'
            ),
            (
                "type=comment AND creator=currentUser() "
                f'AND lastmodified >= "{cql_from}" AND lastmodified <= "{cql_to}"'
            ),
        ]
        for cql in cql_queries:
            for content in self._search(cql):
                content_id = str(content["id"])
                if content_id in seen:
                    continue
                seen.add(content_id)
                self._collect_content(
                    batch,
                    content,
                    account_id,
                    start,
                    end,
                    collected_at,
                )
        return batch

    def _search(self, cql: str) -> list[dict[str, Any]]:
        start_at = 0
        results: list[dict[str, Any]] = []
        while True:
            payload = self.api.get_json(
                "/wiki/rest/api/content/search",
                params={
                    "cql": cql,
                    "start": start_at,
                    "limit": 100,
                    "expand": "body.storage,version,space,history,ancestors",
                },
            )
            page = payload.get("results", [])
            results.extend(page)
            start_at += len(page)
            if not page or start_at >= payload.get("size", 0) + payload.get("start", 0):
                if not payload.get("_links", {}).get("next"):
                    break
            if not payload.get("_links", {}).get("next"):
                break
        return results

    def _collect_content(
        self,
        batch: NormalizedBatch,
        content: dict[str, Any],
        account_id: str,
        start: datetime,
        end: datetime,
        collected_at: datetime,
    ) -> None:
        content_id = str(content["id"])
        content_type = content.get("type", "content")
        version = content.get("version") or {}
        history = content.get("history") or {}
        space = content.get("space") or {}
        webui = content.get("_links", {}).get("webui") or ""
        url = f"{self.site_url}/wiki{webui}" if webui.startswith("/") else None
        body = html_to_text((content.get("body", {}).get("storage") or {}).get("value"))
        created_at = parse_datetime(history.get("createdDate"))
        updated_at = parse_datetime(version.get("when"))
        batch.artifacts.append(
            ArtifactRecord(
                source="confluence",
                remote_id=content_id,
                kind=content_type,
                title=content.get("title") or f"Confluence {content_type} {content_id}",
                body_text=body,
                state=content.get("status"),
                namespace=space.get("key") or space.get("name"),
                url=url,
                created_at=created_at,
                updated_at=updated_at,
                raw=content,
            )
        )
        batch.raw_records.append(
            RawRecordInput(
                source="confluence",
                record_key=f"content:{content_id}:{version.get('number', 0)}",
                kind=content_type,
                payload=content,
                collected_at=collected_at,
            )
        )
        creator_id = (history.get("createdBy") or {}).get("accountId")
        if creator_id == account_id and in_window(created_at, start, end):
            batch.events.append(
                ActivityRecord(
                    source="confluence",
                    event_key=f"content:{content_id}:created",
                    kind=content_type,
                    action="created",
                    occurred_at=created_at,
                    actor_remote_id=account_id,
                    artifact_remote_id=content_id,
                    title=content.get("title") or content_id,
                    url=url,
                )
            )
        current_author = (version.get("by") or {}).get("accountId")
        if current_author == account_id and in_window(updated_at, start, end):
            batch.events.append(
                ActivityRecord(
                    source="confluence",
                    event_key=f"content:{content_id}:version:{version.get('number', 1)}",
                    kind=content_type,
                    action="updated" if (version.get("number") or 1) > 1 else "created",
                    occurred_at=updated_at,
                    actor_remote_id=account_id,
                    artifact_remote_id=content_id,
                    title=content.get("title") or content_id,
                    url=url,
                )
            )
        if content_type in {"page", "blogpost"}:
            self._collect_versions(batch, content_id, content_type, account_id, start, end)
            self._collect_child_comments(
                batch,
                content_id,
                account_id,
                start,
                end,
                url,
                collected_at,
            )

    def _collect_versions(
        self,
        batch: NormalizedBatch,
        content_id: str,
        content_type: str,
        account_id: str,
        start: datetime,
        end: datetime,
    ) -> None:
        resource = "pages" if content_type == "page" else "blogposts"
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 250, "sort": "-modified-date"}
            if cursor:
                params["cursor"] = cursor
            payload = self.api.get_json(
                f"/wiki/api/v2/{resource}/{content_id}/versions",
                params=params,
            )
            versions = payload.get("results", [])
            stop = False
            for version in versions:
                created_at = parse_datetime(version.get("createdAt"))
                if created_at and created_at < start:
                    stop = True
                if version.get("authorId") != account_id or not in_window(created_at, start, end):
                    continue
                number = str(version["number"])
                detail = self.api.get_json(
                    f"/wiki/api/v2/{resource}/{content_id}/versions/{number}",
                    params={"body-format": "storage"},
                )
                body_value = (detail.get("body", {}).get("storage") or {}).get("value")
                batch.versions.append(
                    ArtifactVersionRecord(
                        source="confluence",
                        artifact_remote_id=content_id,
                        remote_version_id=number,
                        author_remote_id=account_id,
                        body_text=html_to_text(body_value),
                        created_at=created_at,
                        raw=detail,
                    )
                )
                batch.events.append(
                    ActivityRecord(
                        source="confluence",
                        event_key=f"content:{content_id}:version:{number}",
                        kind=content_type,
                        action="updated" if int(number) > 1 else "created",
                        occurred_at=created_at,
                        actor_remote_id=account_id,
                        artifact_remote_id=content_id,
                        title=detail.get("title") or content_id,
                        url=None,
                    )
                )
            next_link = payload.get("_links", {}).get("next")
            if stop or not next_link:
                break
            cursor = next_link.split("cursor=", 1)[-1].split("&", 1)[0]

    def _collect_child_comments(
        self,
        batch: NormalizedBatch,
        content_id: str,
        account_id: str,
        start: datetime,
        end: datetime,
        parent_url: str | None,
        collected_at: datetime,
    ) -> None:
        start_at = 0
        while True:
            try:
                payload = self.api.get_json(
                    f"/wiki/rest/api/content/{content_id}/child/comment",
                    params={
                        "start": start_at,
                        "limit": 100,
                        "expand": "body.storage,version,history",
                    },
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {403, 404}:
                    return
                raise
            comments = payload.get("results", [])
            for comment in comments:
                comment_id = str(comment["id"])
                version = comment.get("version") or {}
                history = comment.get("history") or {}
                author_id = (history.get("createdBy") or {}).get("accountId")
                created_at = parse_datetime(history.get("createdDate"))
                updated_at = parse_datetime(version.get("when"))
                body = html_to_text((comment.get("body", {}).get("storage") or {}).get("value"))
                batch.versions.append(
                    ArtifactVersionRecord(
                        source="confluence",
                        artifact_remote_id=content_id,
                        remote_version_id=f"comment:{comment_id}:{version.get('number', 1)}",
                        author_remote_id=author_id,
                        body_text=body,
                        created_at=updated_at or created_at,
                        raw=comment,
                    )
                )
                if author_id == account_id and in_window(updated_at or created_at, start, end):
                    batch.events.append(
                        ActivityRecord(
                            source="confluence",
                            event_key=f"comment:{comment_id}:{version.get('number', 1)}",
                            kind="comment",
                            action="commented"
                            if (version.get("number") or 1) == 1
                            else "comment_edited",
                            occurred_at=updated_at or created_at,
                            actor_remote_id=account_id,
                            artifact_remote_id=content_id,
                            title=f"Comment on {content_id}",
                            changes={"body": body},
                            url=parent_url,
                        )
                    )
                batch.raw_records.append(
                    RawRecordInput(
                        source="confluence",
                        record_key=f"comment:{comment_id}:{version.get('number', 1)}",
                        kind="comment",
                        payload=comment,
                        collected_at=collected_at,
                    )
                )
            start_at += len(comments)
            if not comments or not payload.get("_links", {}).get("next"):
                break
