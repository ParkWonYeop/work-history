"""Local stdio MCP server for reading work history and writing reports with an LLM client.

It runs on the Mac next to the Report Agent and never exposes a network port. Report context,
missing periods and uploads use the Report Agent's signed device key; activity, artifact and
stored-report reads use the read API token kept in the macOS Keychain.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import time as clock
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import keyring

from work_history.report_agent import (
    DEFAULT_CONFIG,
    KEYRING_SERVICE,
    ReportServerClient,
    _client,
    _context_status,
    _load_config,
    upload_document,
)

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - optional dependency guard
    raise ImportError("Install the MCP extra: pip install 'work-history[mcp]'") from exc

SEOUL = ZoneInfo("Asia/Seoul")
PROMPT_VERSION = "work-history-mcp-v1"
READ_TOKEN_ACCOUNT = "read-api-token"
MAX_RESPONSE_CHARS = 60_000
MAX_BODY_CHARS = 8_000
MAX_CHANGE_CHARS = 1_000
MAX_SEARCH_DAYS = 93
CACHED_CONTEXTS = 4

Cadence = Literal["daily", "weekly", "monthly", "overall"]
Kind = Literal["work_report", "feedback"]
Source = Literal["jira", "confluence", "gitlab", "slack"]

INSTRUCTIONS = """\
Work History holds the user's own Jira, Confluence, GitLab and Slack activity. All times are
Asia/Seoul. Periods: daily YYYY-MM-DD, weekly YYYY-Www, monthly YYYY-MM, overall
YYYY-MM-DD_to_YYYY-MM-DD.

To write a report: find periods with list_missing_reports (or take the one the user names), call
get_report_context with section="summary", page through "activities", "artifacts" and, for
weekly or monthly reports, "daily_documents" using next_offset, then write Korean Markdown and
save both kinds with put_report.

Only actor_is_self=true events are the user's own work; other Slack messages are collaboration
context. Cite source URLs near claims, separate observation from inference, and never treat event
counts as productivity. If upload_status is "partial", say which sources are stale.

work_report sections: 종합 정리, 주요 업무 한눈에 보기, 시간순 업무 진행, 업무별 상세 내용,
결정·협업·문서화, 문제·장애물·미해결 사항, 다음 작업 및 우선순위, 데이터 완전성 및 근거.
feedback sections: 종합 평가, 잘한 점과 유지할 업무 방식, 개선할 점과 원인, 추천 개선 방법,
앞으로의 업무 진행 방향, 다음 근무일 실행 항목, 1~2주 개선 실험, 평가의 한계.
"""


def _api_error(response: httpx.Response) -> ToolError:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = response.text[:300]
    return ToolError(f"work-history API {response.status_code}: {detail}")


class Backend:
    def __init__(
        self,
        config_path: Path,
        signed: ReportServerClient | None = None,
        read: httpx.Client | None = None,
    ) -> None:
        self.config_path = config_path
        self._signed = signed
        self._read = read
        self.contexts: dict[tuple[str, str], dict[str, Any]] = {}

    def _signed_client(self) -> ReportServerClient:
        if self._signed is None:
            try:
                self._signed = _client(self.config_path)
            except (OSError, KeyError, RuntimeError, ValueError) as exc:
                raise ToolError(f"Report Agent config or Keychain key is unusable: {exc}") from exc
        return self._signed

    def _read_client(self) -> httpx.Client:
        if self._read is None:
            try:
                config = _load_config(self.config_path)
            except OSError as exc:
                raise ToolError(f"Report Agent config is unreadable: {exc}") from exc
            token = os.getenv("WORK_HISTORY_READ_API_TOKEN") or keyring.get_password(
                KEYRING_SERVICE, f"{config['device_id']}:{READ_TOKEN_ACCOUNT}"
            )
            if not token:
                raise ToolError("read API token is missing; run `work-history-mcp set-read-token`")
            self._read = httpx.Client(
                base_url=str(config["server_url"]).rstrip("/"),
                headers={"Authorization": f"Bearer {token}", "User-Agent": "work-history-mcp/0.1"},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._read

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = self._read_client()
        for attempt in range(4):
            try:
                response = client.get(path, params=params)
            except httpx.TransportError as exc:
                raise ToolError(f"work-history API is unreachable: {exc}") from exc
            if response.status_code != 429:
                break
            clock.sleep(5 * (attempt + 1))  # the read API allows 30 requests per minute
        if not response.is_success:
            raise _api_error(response)
        return response.json()

    def signed(self, method: Literal["post", "put"], path: str, payload: dict[str, Any]) -> Any:
        client = self._signed_client()
        try:
            return getattr(client, method)(path, payload)
        except httpx.HTTPStatusError as exc:
            raise _api_error(exc.response) from exc
        except httpx.TransportError as exc:
            raise ToolError(f"work-history API is unreachable: {exc}") from exc

    def context(self, cadence: str, period: str, refresh: bool = False) -> dict[str, Any]:
        key = (cadence, period)
        if refresh or key not in self.contexts:
            self.contexts.pop(key, None)
            self.contexts[key] = self.signed(
                "post", "/v1/report-agent/context", {"cadence": cadence, "period": period}
            )
            while len(self.contexts) > CACHED_CONTEXTS:
                self.contexts.pop(next(iter(self.contexts)))
        return self.contexts[key]

    def upload(self, cadence: str, period: str, kind: str, **fields: Any) -> dict[str, Any]:
        context = self.context(cadence, period)
        client = self._signed_client()
        try:
            return upload_document(client, cadence, period, kind, context=context, **fields)
        except httpx.HTTPStatusError as exc:
            raise _api_error(exc.response) from exc
        except httpx.TransportError as exc:
            raise ToolError(f"work-history API is unreachable: {exc}") from exc


def _kst(value: Any) -> Any:
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(SEOUL).isoformat()


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]} …[{len(value) - limit}자 생략]"
    if isinstance(value, dict):
        return {key: _clip(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip(item, limit) for item in value]
    return value


def _page(items: list[Any], offset: int, limit: int) -> dict[str, Any]:
    """Slice items and stop early so one tool response stays within the character budget."""
    offset = max(offset, 0)
    selected: list[Any] = []
    used = 0
    for item in items[offset : offset + max(1, min(limit, 500))]:
        size = len(json.dumps(item, ensure_ascii=False, default=str))
        if selected and used + size > MAX_RESPONSE_CHARS:
            break
        selected.append(item)
        used += size
    end = offset + len(selected)
    return {"items": selected, "offset": offset, "next_offset": end if end < len(items) else None}


def _activity(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "occurred_at": _kst(item.get("occurred_at")),
        "source": item.get("source"),
        "kind": item.get("kind"),
        "action": item.get("action"),
        "actor_is_self": item.get("actor_is_self"),
        "title": item.get("title"),
        "artifact_remote_id": item.get("artifact_remote_id"),
        "url": item.get("url"),
        "changes": _clip(item.get("changes") or {}, MAX_CHANGE_CHARS),
    }


def _artifact(item: dict[str, Any], version_limit: int = 20) -> dict[str, Any]:
    versions = item.get("versions") or []
    return {
        "source": item.get("source"),
        "remote_id": item.get("remote_id"),
        "kind": item.get("kind"),
        "title": item.get("title"),
        "state": item.get("state"),
        "namespace": item.get("namespace"),
        "url": item.get("url"),
        "created_at": _kst(item.get("created_at_remote")),
        "updated_at": _kst(item.get("updated_at_remote")),
        "body_text": _clip(item.get("body_text"), MAX_BODY_CHARS),
        "versions": [
            {
                "remote_version_id": version.get("remote_version_id"),
                "author_remote_id": version.get("author_remote_id"),
                "created_at": _kst(version.get("created_at_remote")),
                "body_text": _clip(version.get("body_text"), 2_000),
            }
            for version in versions[-version_limit:]
        ],
        "omitted_versions": max(0, len(versions) - version_limit),
    }


def _daily_document(item: dict[str, Any]) -> dict[str, Any]:
    return {**item, "markdown": _clip(item.get("markdown"), 20_000)}


def _seoul_midnight(value: date) -> str:
    return datetime.combine(value, time.min, SEOUL).isoformat()


def build_server(backend: Backend) -> MCPServer:
    server = MCPServer("work-history", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    @server.tool(annotations=read_only)
    def get_report_context(
        cadence: Cadence,
        period: str,
        section: Literal["summary", "activities", "artifacts", "daily_documents"] = "summary",
        offset: int = 0,
        limit: int = 100,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Evidence for one report period. Start with section="summary" (counts, source
        freshness, upload_status, existing reports), then page other sections with next_offset.
        The context is cached per period; pass refresh=true to fetch it again."""
        context = backend.context(cadence, period, refresh)
        if section == "summary":
            keys = (
                "cadence",
                "period",
                "period_start",
                "period_end",
                "activity_count",
                "source_event_counts",
                "source_total_event_counts",
                "omitted_activity_count",
                "source_snapshot",
                "all_sources_fresh",
                "redaction_count",
                "truncated",
                "existing",
            )
            return {
                **{key: context.get(key) for key in keys},
                "from_time": _kst(context.get("from_time")),
                "to_time": _kst(context.get("to_time")),
                "upload_status": _context_status(context),
                "sections": {
                    name: len(context.get(name) or [])
                    for name in ("activities", "artifacts", "daily_documents")
                },
            }
        shape = {
            "activities": _activity,
            "artifacts": _artifact,
            "daily_documents": _daily_document,
        }[section]
        items = [shape(item) for item in context.get(section) or []]
        return {"section": section, "total": len(items), **_page(items, offset, limit)}

    @server.tool(annotations=read_only)
    def list_missing_reports(
        cadence: Literal["daily", "weekly", "monthly"],
        from_date: str,
        to_date: str,
        include_partial: bool = True,
    ) -> dict[str, Any]:
        """Periods between two dates (YYYY-MM-DD) that lack a report, or whose partial report
        is outdated because the sources have since caught up."""
        return backend.signed(
            "post",
            "/v1/report-agent/missing",
            {
                "cadence": cadence,
                "from": from_date,
                "to": to_date,
                "include_partial": include_partial,
            },
        )

    @server.tool(annotations=read_only)
    def search_activities(
        from_date: str,
        to_date: str,
        sources: list[Source] | None = None,
        query: str | None = None,
        self_only: bool = False,
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Collected activities between two Asia/Seoul dates (YYYY-MM-DD, inclusive, at most
        93 days), oldest first. query matches title, artifact id and change details without
        case sensitivity; self_only keeps the user's own actions."""
        try:
            start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
        except ValueError as exc:
            raise ToolError("dates must use YYYY-MM-DD") from exc
        if end < start or (end - start).days >= MAX_SEARCH_DAYS:
            raise ToolError(f"to_date must be within {MAX_SEARCH_DAYS} days after from_date")
        offset, limit = max(offset, 0), max(1, min(limit, 500))
        needle = query.casefold() if query else None
        wanted = offset + limit + 1
        matches: list[dict[str, Any]] = []
        chunk_start = start
        while chunk_start <= end and len(matches) < wanted:
            chunk_end = min(chunk_start + timedelta(days=30), end)  # the API allows 31 days
            params: dict[str, Any] = {
                "from": _seoul_midnight(chunk_start),
                "to": _seoul_midnight(chunk_end + timedelta(days=1)),
                "limit": 500,
            }
            if sources:
                params["sources"] = ",".join(sources)
            while len(matches) < wanted:
                page = backend.get("/v1/activities", params)
                for item in page.get("items", []):
                    if self_only and not item.get("actor_is_self"):
                        continue
                    if needle:
                        text = " ".join(
                            [
                                str(item.get("title") or ""),
                                str(item.get("artifact_remote_id") or ""),
                                json.dumps(item.get("changes") or {}, ensure_ascii=False),
                            ]
                        )
                        if needle not in text.casefold():
                            continue
                    matches.append(_activity(item))
                if not page.get("next_cursor"):
                    break
                params["cursor"] = page["next_cursor"]
            chunk_start = chunk_end + timedelta(days=1)
        return _page(matches[:wanted], offset, limit)

    @server.tool(annotations=read_only)
    def get_artifact(source: Source, remote_id: str) -> dict[str, Any]:
        """One Jira issue, Confluence page, GitLab MR/commit/issue or Slack message with its
        stored body and latest versions. remote_id is artifact_remote_id from an activity."""
        return _artifact(backend.get(f"/v1/artifacts/{source}/{quote(remote_id, safe=':')}"))

    @server.tool(annotations=read_only)
    def get_report(cadence: Cadence, period: str, kind: Kind) -> dict[str, Any]:
        """A stored report's current Markdown and revision metadata."""
        report = backend.get(f"/v1/reports/{cadence}/{quote(period, safe='')}/{kind}")
        return {
            **{
                key: report.get(key)
                for key in (
                    "cadence",
                    "period",
                    "kind",
                    "status",
                    "title",
                    "markdown",
                    "current_revision",
                    "prompt_version",
                    "generator_model",
                )
            },
            "updated_at": _kst(report.get("updated_at")),
            "finalized_at": _kst(report.get("finalized_at")),
            "revisions": len(report.get("versions") or []),
        }

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def put_report(
        cadence: Cadence,
        period: str,
        kind: Kind,
        title: str,
        markdown: str,
        generator_model: str,
        prompt_version: str = PROMPT_VERSION,
    ) -> dict[str, Any]:
        """Store a work_report or feedback. The source snapshot and final/partial status come
        from the context last fetched for this period, so the report records the evidence it was
        written from. Every change becomes a new revision; identical content is a no-op.
        generator_model is the model writing the report."""
        report = backend.upload(
            cadence,
            period,
            kind,
            title=title,
            markdown=markdown,
            prompt_version=prompt_version,
            generator_model=generator_model,
        )
        return {
            **{
                key: report.get(key)
                for key in ("cadence", "period", "kind", "status", "title", "current_revision")
            },
            "content_sha256": report.get("content_sha256"),
            "updated_at": _kst(report.get("updated_at")),
        }

    return server


def set_read_token(config_path: Path) -> None:
    config = _load_config(config_path)
    token = getpass.getpass("Work History read API token: ").strip()
    if not token:
        raise RuntimeError("the read API token may not be empty")
    keyring.set_password(KEYRING_SERVICE, f"{config['device_id']}:{READ_TOKEN_ACCOUNT}", token)
    print("Stored the read API token in the macOS Keychain.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local MCP server for work-history reports")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("command", nargs="?", choices=["serve", "set-read-token"], default="serve")
    args = parser.parse_args()
    if args.command == "set-read-token":
        set_read_token(args.config)
        return
    build_server(Backend(args.config)).run("stdio")


if __name__ == "__main__":
    main()
