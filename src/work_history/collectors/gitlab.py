from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from work_history.collectors.common import ApiClient, in_window, parse_datetime
from work_history.schemas import (
    ActivityRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    IdentityRecord,
    NormalizedBatch,
    RawRecordInput,
)

logger = logging.getLogger(__name__)


class GitLabCollector:
    def __init__(self, gitlab_url: str, private_token: str) -> None:
        client = httpx.Client(
            base_url=gitlab_url.rstrip("/"),
            headers={"PRIVATE-TOKEN": private_token, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
        self.gitlab_url = gitlab_url.rstrip("/")
        self.api = ApiClient(client)

    def close(self) -> None:
        self.api.client.close()

    def check_connection(self) -> dict[str, Any]:
        return self.api.get_json("/api/v4/user")

    def collect(self, start: datetime, end: datetime) -> NormalizedBatch:
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        user = self.check_connection()
        user_id = str(user["id"])
        emails = {value for value in [user.get("email"), user.get("public_email")] if value}
        try:
            emails.update(item["email"] for item in self.api.get_json("/api/v4/user/emails"))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {401, 403, 404}:
                raise
        batch = NormalizedBatch(
            identities=[
                IdentityRecord(
                    source="gitlab",
                    remote_id=user_id,
                    username=user.get("username"),
                    display_name=user.get("name"),
                    email=user.get("email"),
                    raw=user,
                )
            ]
        )
        collected_at = datetime.now(UTC)
        project_ids: set[int] = set()
        self._collect_events(batch, user_id, start, end, collected_at, project_ids)
        self._collect_merge_requests(batch, user, emails, start, end, collected_at, project_ids)
        self._collect_issues(batch, user, start, end, collected_at, project_ids)
        self._collect_project_commits(batch, user, emails, start, end, collected_at, project_ids)
        return batch

    def _paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        tolerate: set[int] | None = None,
    ) -> Iterable[dict[str, Any]]:
        page = 1
        while True:
            query = dict(params or {})
            query.update({"page": page, "per_page": 100})
            try:
                response = self.api.request("GET", path, params=query)
            except httpx.HTTPStatusError as exc:
                if tolerate and exc.response.status_code in tolerate:
                    return
                raise
            payload = response.json()
            if not isinstance(payload, list):
                return
            yield from payload
            next_page = response.headers.get("X-Next-Page")
            if next_page:
                page = int(next_page)
            elif len(payload) == 100:
                page += 1
            else:
                break

    def _collect_events(
        self,
        batch: NormalizedBatch,
        user_id: str,
        start: datetime,
        end: datetime,
        collected_at: datetime,
        project_ids: set[int],
    ) -> None:
        params = {
            "after": start.strftime("%Y-%m-%d"),
            "before": (end + timedelta(days=1)).date().isoformat(),
            "scope": "all",
            "sort": "asc",
        }
        for event in self._paginate(f"/api/v4/users/{user_id}/events", params):
            occurred_at = parse_datetime(event.get("created_at"))
            if not in_window(occurred_at, start, end):
                continue
            project_id = event.get("project_id")
            if project_id:
                project_ids.add(int(project_id))
            target_type = (event.get("target_type") or "activity").lower()
            target_iid = event.get("target_iid") or event.get("target_id")
            artifact_remote_id = (
                f"project:{project_id}:{target_type}:{target_iid}"
                if project_id and target_iid
                else None
            )
            batch.events.append(
                ActivityRecord(
                    source="gitlab",
                    event_key=f"event:{event['id']}",
                    kind=target_type,
                    action=event.get("action_name") or "activity",
                    occurred_at=occurred_at,
                    actor_remote_id=user_id,
                    artifact_remote_id=artifact_remote_id,
                    title=event.get("target_title")
                    or event.get("push_data", {}).get("commit_title")
                    or "GitLab activity",
                    changes=event.get("push_data") or {},
                    url=None,
                    raw=event,
                )
            )
            batch.raw_records.append(
                RawRecordInput(
                    source="gitlab",
                    record_key=f"event:{event['id']}",
                    kind="event",
                    payload=event,
                    collected_at=collected_at,
                )
            )

    def _collect_merge_requests(
        self,
        batch: NormalizedBatch,
        user: dict[str, Any],
        emails: set[str],
        start: datetime,
        end: datetime,
        collected_at: datetime,
        project_ids: set[int],
    ) -> None:
        user_id = int(user["id"])
        base = {
            "scope": "all",
            "state": "all",
            "updated_after": start.isoformat(),
            "updated_before": end.isoformat(),
            "order_by": "updated_at",
            "sort": "asc",
        }
        filters = [
            {"author_id": user_id},
            {"assignee_id": user_id},
            {"reviewer_id": user_id},
            {"merge_user_id": user_id},
        ]
        merge_requests: dict[tuple[int, int], dict[str, Any]] = {}
        for extra in filters:
            for mr in self._paginate(
                "/api/v4/merge_requests",
                {**base, **extra},
                tolerate={400},
            ):
                merge_requests[(int(mr["project_id"]), int(mr["iid"]))] = mr
        for (project_id, iid), mr in merge_requests.items():
            project_ids.add(project_id)
            remote_id = f"project:{project_id}:mr:{iid}"
            batch.artifacts.append(
                ArtifactRecord(
                    source="gitlab",
                    remote_id=remote_id,
                    kind="merge_request",
                    title=mr.get("title") or f"MR !{iid}",
                    body_text=mr.get("description") or "",
                    state=mr.get("state"),
                    namespace=(mr.get("references") or {}).get("full"),
                    url=mr.get("web_url"),
                    created_at=parse_datetime(mr.get("created_at")),
                    updated_at=parse_datetime(mr.get("updated_at")),
                    raw=mr,
                )
            )
            batch.raw_records.append(
                RawRecordInput(
                    source="gitlab",
                    record_key=f"mr:{project_id}:{iid}:{mr.get('updated_at')}",
                    kind="merge_request",
                    payload=mr,
                    collected_at=collected_at,
                )
            )
            created_at = parse_datetime(mr.get("created_at"))
            if (mr.get("author") or {}).get("id") == user_id and in_window(created_at, start, end):
                batch.events.append(
                    ActivityRecord(
                        source="gitlab",
                        event_key=f"mr:{project_id}:{iid}:created",
                        kind="merge_request",
                        action="created",
                        occurred_at=created_at,
                        actor_remote_id=str(user_id),
                        artifact_remote_id=remote_id,
                        title=mr.get("title") or f"MR !{iid}",
                        url=mr.get("web_url"),
                    )
                )
            merged_at = parse_datetime(mr.get("merged_at"))
            merge_user = mr.get("merge_user") or mr.get("merged_by") or {}
            if merge_user.get("id") == user_id and in_window(merged_at, start, end):
                batch.events.append(
                    ActivityRecord(
                        source="gitlab",
                        event_key=f"mr:{project_id}:{iid}:merged",
                        kind="merge_request",
                        action="merged",
                        occurred_at=merged_at,
                        actor_remote_id=str(user_id),
                        artifact_remote_id=remote_id,
                        title=mr.get("title") or f"MR !{iid}",
                        url=mr.get("web_url"),
                    )
                )
            self._collect_mr_discussions(
                batch, project_id, iid, remote_id, user_id, start, end, mr.get("web_url")
            )
            self._collect_mr_commits(
                batch, project_id, iid, user, emails, start, end, mr.get("web_url")
            )
            try:
                approvals = self.api.get_json(
                    f"/api/v4/projects/{project_id}/merge_requests/{iid}/approvals"
                )
                batch.raw_records.append(
                    RawRecordInput(
                        source="gitlab",
                        record_key=f"mr:{project_id}:{iid}:approvals:{mr.get('updated_at')}",
                        kind="merge_request_approvals",
                        payload=approvals,
                        collected_at=collected_at,
                    )
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {403, 404}:
                    raise

    def _collect_mr_discussions(
        self,
        batch: NormalizedBatch,
        project_id: int,
        iid: int,
        remote_id: str,
        user_id: int,
        start: datetime,
        end: datetime,
        url: str | None,
    ) -> None:
        for discussion in self._paginate(
            f"/api/v4/projects/{project_id}/merge_requests/{iid}/discussions",
            tolerate={403, 404},
        ):
            for note in discussion.get("notes", []):
                if (note.get("author") or {}).get("id") != user_id:
                    continue
                occurred_at = parse_datetime(note.get("updated_at") or note.get("created_at"))
                if not in_window(occurred_at, start, end):
                    continue
                note_id = str(note["id"])
                body = note.get("body") or ""
                batch.versions.append(
                    ArtifactVersionRecord(
                        source="gitlab",
                        artifact_remote_id=remote_id,
                        remote_version_id=f"note:{note_id}:{note.get('updated_at')}",
                        author_remote_id=str(user_id),
                        body_text=body,
                        created_at=occurred_at,
                        raw=note,
                    )
                )
                batch.events.append(
                    ActivityRecord(
                        source="gitlab",
                        event_key=f"mr:{project_id}:{iid}:note:{note_id}:{note.get('updated_at')}",
                        kind="merge_request_note",
                        action="system_action" if note.get("system") else "commented",
                        occurred_at=occurred_at,
                        actor_remote_id=str(user_id),
                        artifact_remote_id=remote_id,
                        title=f"MR !{iid} note",
                        changes={"body": body, "system": bool(note.get("system"))},
                        url=url,
                        raw=note,
                    )
                )

    def _collect_mr_commits(
        self,
        batch: NormalizedBatch,
        project_id: int,
        iid: int,
        user: dict[str, Any],
        emails: set[str],
        start: datetime,
        end: datetime,
        url: str | None,
    ) -> None:
        commits = self.api.get_json(f"/api/v4/projects/{project_id}/merge_requests/{iid}/commits")
        for commit in commits:
            if not self._commit_is_self(commit, user, emails):
                continue
            occurred_at = parse_datetime(
                commit.get("authored_date") or commit.get("committed_date")
            )
            if not in_window(occurred_at, start, end):
                continue
            self._append_commit(batch, project_id, commit, str(user["id"]), url)

    def _collect_issues(
        self,
        batch: NormalizedBatch,
        user: dict[str, Any],
        start: datetime,
        end: datetime,
        collected_at: datetime,
        project_ids: set[int],
    ) -> None:
        user_id = int(user["id"])
        base = {
            "scope": "all",
            "state": "all",
            "updated_after": start.isoformat(),
            "updated_before": end.isoformat(),
            "order_by": "updated_at",
            "sort": "asc",
        }
        issues: dict[tuple[int, int], dict[str, Any]] = {}
        for extra in ({"author_id": user_id}, {"assignee_id": user_id}):
            for issue in self._paginate("/api/v4/issues", {**base, **extra}, tolerate={400}):
                issues[(int(issue["project_id"]), int(issue["iid"]))] = issue
        for (project_id, iid), issue in issues.items():
            project_ids.add(project_id)
            remote_id = f"project:{project_id}:issue:{iid}"
            batch.artifacts.append(
                ArtifactRecord(
                    source="gitlab",
                    remote_id=remote_id,
                    kind="issue",
                    title=issue.get("title") or f"Issue #{iid}",
                    body_text=issue.get("description") or "",
                    state=issue.get("state"),
                    namespace=(issue.get("references") or {}).get("full"),
                    url=issue.get("web_url"),
                    created_at=parse_datetime(issue.get("created_at")),
                    updated_at=parse_datetime(issue.get("updated_at")),
                    raw=issue,
                )
            )
            batch.raw_records.append(
                RawRecordInput(
                    source="gitlab",
                    record_key=f"issue:{project_id}:{iid}:{issue.get('updated_at')}",
                    kind="issue",
                    payload=issue,
                    collected_at=collected_at,
                )
            )
            created_at = parse_datetime(issue.get("created_at"))
            if (issue.get("author") or {}).get("id") == user_id and in_window(
                created_at, start, end
            ):
                batch.events.append(
                    ActivityRecord(
                        source="gitlab",
                        event_key=f"issue:{project_id}:{iid}:created",
                        kind="issue",
                        action="created",
                        occurred_at=created_at,
                        actor_remote_id=str(user_id),
                        artifact_remote_id=remote_id,
                        title=issue.get("title") or f"Issue #{iid}",
                        url=issue.get("web_url"),
                    )
                )
            for note in self._paginate(
                f"/api/v4/projects/{project_id}/issues/{iid}/notes",
                {"sort": "asc", "order_by": "created_at"},
                tolerate={403, 404},
            ):
                if (note.get("author") or {}).get("id") != user_id:
                    continue
                occurred_at = parse_datetime(note.get("updated_at") or note.get("created_at"))
                if not in_window(occurred_at, start, end):
                    continue
                note_id = str(note["id"])
                body = note.get("body") or ""
                batch.versions.append(
                    ArtifactVersionRecord(
                        source="gitlab",
                        artifact_remote_id=remote_id,
                        remote_version_id=f"note:{note_id}:{note.get('updated_at')}",
                        author_remote_id=str(user_id),
                        body_text=body,
                        created_at=occurred_at,
                        raw=note,
                    )
                )
                batch.events.append(
                    ActivityRecord(
                        source="gitlab",
                        event_key=f"issue:{project_id}:{iid}:note:{note_id}:{note.get('updated_at')}",
                        kind="issue_note",
                        action="system_action" if note.get("system") else "commented",
                        occurred_at=occurred_at,
                        actor_remote_id=str(user_id),
                        artifact_remote_id=remote_id,
                        title=issue.get("title") or f"Issue #{iid}",
                        changes={"body": body, "system": bool(note.get("system"))},
                        url=issue.get("web_url"),
                        raw=note,
                    )
                )

    def _collect_project_commits(
        self,
        batch: NormalizedBatch,
        user: dict[str, Any],
        emails: set[str],
        start: datetime,
        end: datetime,
        collected_at: datetime,
        project_ids: set[int],
    ) -> None:
        if end - start >= timedelta(days=6) or not project_ids:
            for project in self._paginate(
                "/api/v4/projects",
                {"membership": "true", "simple": "true", "order_by": "last_activity_at"},
                tolerate={403},
            ):
                project_ids.add(int(project["id"]))
        for project_id in sorted(project_ids):
            params = {
                "since": start.isoformat(),
                "until": end.isoformat(),
                "all": "true",
                "with_stats": "false",
            }
            for commit in self._paginate(
                f"/api/v4/projects/{project_id}/repository/commits",
                params,
                tolerate={403, 404},
            ):
                if not self._commit_is_self(commit, user, emails):
                    continue
                occurred_at = parse_datetime(
                    commit.get("authored_date") or commit.get("committed_date")
                )
                if not in_window(occurred_at, start, end):
                    continue
                self._append_commit(
                    batch,
                    project_id,
                    commit,
                    str(user["id"]),
                    commit.get("web_url"),
                )
                batch.raw_records.append(
                    RawRecordInput(
                        source="gitlab",
                        record_key=f"commit:{project_id}:{commit['id']}",
                        kind="commit",
                        payload=commit,
                        collected_at=collected_at,
                    )
                )

    @staticmethod
    def _commit_is_self(commit: dict[str, Any], user: dict[str, Any], emails: set[str]) -> bool:
        author_email = (commit.get("author_email") or "").lower()
        committer_email = (commit.get("committer_email") or "").lower()
        lowered_emails = {value.lower() for value in emails}
        if author_email in lowered_emails or committer_email in lowered_emails:
            return True
        names = {user.get("name"), user.get("username")}
        return commit.get("author_name") in names or commit.get("committer_name") in names

    @staticmethod
    def _append_commit(
        batch: NormalizedBatch,
        project_id: int,
        commit: dict[str, Any],
        user_id: str,
        url: str | None,
    ) -> None:
        sha = str(commit["id"])
        remote_id = f"project:{project_id}:commit:{sha}"
        occurred_at = parse_datetime(commit.get("authored_date") or commit.get("committed_date"))
        batch.artifacts.append(
            ArtifactRecord(
                source="gitlab",
                remote_id=remote_id,
                kind="commit",
                title=commit.get("title") or sha[:8],
                body_text=commit.get("message") or "",
                state="committed",
                namespace=str(project_id),
                url=commit.get("web_url") or url,
                created_at=occurred_at,
                updated_at=occurred_at,
                raw=commit,
            )
        )
        batch.events.append(
            ActivityRecord(
                source="gitlab",
                event_key=f"commit:{project_id}:{sha}",
                kind="commit",
                action="committed",
                occurred_at=occurred_at,
                actor_remote_id=user_id,
                artifact_remote_id=remote_id,
                title=commit.get("title") or sha[:8],
                url=commit.get("web_url") or url,
            )
        )
