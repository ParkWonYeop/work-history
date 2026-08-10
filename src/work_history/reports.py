from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from work_history.models import (
    ActivityEvent,
    Artifact,
    GeneratedReport,
    GeneratedReportVersion,
    SyncCursor,
    utcnow,
)

ReportCadence = Literal["daily", "weekly", "monthly", "overall"]
ScheduledReportCadence = Literal["daily", "weekly", "monthly"]
ParsedPeriod = date | tuple[date, date]
ReportKind = Literal["work_report", "feedback"]

REPORT_KINDS: tuple[ReportKind, ...] = ("work_report", "feedback")
SOURCES = ("jira", "confluence", "gitlab")
SEOUL = ZoneInfo("Asia/Seoul")
MAX_CONTEXT_CHARS = 1_000_000
MAX_EVENTS = 5_000
MAX_BODY_CHARS = 20_000

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+")
_NAMED_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?token|access[_-]?token|client[_-]?secret|password|passwd|secret)"
    r"\b\s*[:=]\s*)([^\s,;\"']+)"
)
_KNOWN_TOKEN = re.compile(r"\b(?:glpat-[A-Za-z0-9_-]{12,}|ATATT[A-Za-z0-9_-]{12,})\b")
_SECRET_KEY = re.compile(
    r"(?i)^(?:api[_-]?token|access[_-]?token|client[_-]?secret|password|passwd|secret)$"
)


def period_bounds(cadence: ReportCadence, period: ParsedPeriod) -> tuple[date, date, str]:
    if cadence == "daily":
        if not isinstance(period, date):
            raise ValueError("daily period must be a date")
        return period, period + timedelta(days=1), period.isoformat()
    if cadence == "weekly":
        if not isinstance(period, date):
            raise ValueError("weekly period must be a date")
        start = period - timedelta(days=period.weekday())
        iso_year, iso_week, _ = start.isocalendar()
        return start, start + timedelta(days=7), f"{iso_year}-W{iso_week:02d}"
    if cadence == "overall":
        if not isinstance(period, tuple):
            raise ValueError("overall period must be a date range")
        start, inclusive_end = period
        if inclusive_end < start:
            raise ValueError("overall period end must be on or after start")
        if (inclusive_end - start).days > 730:
            raise ValueError("overall period may not exceed 730 days")
        return start, inclusive_end + timedelta(days=1), f"{start}_to_{inclusive_end}"
    if not isinstance(period, date):
        raise ValueError("monthly period must be a date")
    start = period.replace(day=1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end, start.strftime("%Y-%m")


def period_datetimes(cadence: ReportCadence, period: ParsedPeriod) -> tuple[datetime, datetime]:
    start, end, _ = period_bounds(cadence, period)
    return (
        datetime.combine(start, datetime.min.time(), SEOUL).astimezone(UTC),
        datetime.combine(end, datetime.min.time(), SEOUL).astimezone(UTC),
    )


def parse_period(cadence: ReportCadence, value: str) -> ParsedPeriod:
    try:
        if cadence == "daily":
            parsed = date.fromisoformat(value)
            if value != parsed.isoformat():
                raise ValueError
            return parsed
        if cadence == "weekly":
            match = re.fullmatch(r"(\d{4})-W(\d{2})", value)
            if not match:
                raise ValueError
            parsed = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
            iso_year, iso_week, _ = parsed.isocalendar()
            if value != f"{iso_year}-W{iso_week:02d}":
                raise ValueError
            return parsed
        if cadence == "overall":
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})", value)
            if not match:
                raise ValueError
            start = date.fromisoformat(match.group(1))
            end = date.fromisoformat(match.group(2))
            if end < start or (end - start).days > 730:
                raise ValueError
            return start, end
        if not re.fullmatch(r"\d{4}-\d{2}", value):
            raise ValueError
        parsed = date.fromisoformat(f"{value}-01")
        return parsed
    except ValueError as exc:
        expected = {
            "daily": "YYYY-MM-DD",
            "weekly": "YYYY-Www",
            "monthly": "YYYY-MM",
            "overall": "YYYY-MM-DD_to_YYYY-MM-DD",
        }[cadence]
        raise ValueError(f"period must use {expected}") from exc


def stored_period_key(cadence: ReportCadence, start: date, exclusive_end: date) -> str:
    if cadence == "daily":
        return start.isoformat()
    if cadence == "weekly":
        iso_year, iso_week, _ = start.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if cadence == "monthly":
        return start.strftime("%Y-%m")
    return f"{start}_to_{exclusive_end - timedelta(days=1)}"


def snapshot_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class ContextRedactor:
    def __init__(self) -> None:
        self.redaction_count = 0
        self.used_chars = 0
        self.truncated = False

    def text(self, value: str | None, maximum: int = MAX_BODY_CHARS) -> str | None:
        if value is None:
            return None
        replacements = 0

        def replace_secret(match: re.Match[str]) -> str:
            nonlocal replacements
            replacements += 1
            prefix = match.group(1) if match.lastindex else ""
            return f"{prefix}[REDACTED]"

        result, count = _PRIVATE_KEY.subn("[REDACTED PRIVATE KEY]", value)
        replacements += count
        result = _AUTHORIZATION.sub(replace_secret, result)
        result = _NAMED_SECRET.sub(replace_secret, result)
        result, count = _KNOWN_TOKEN.subn("[REDACTED TOKEN]", result)
        replacements += count
        self.redaction_count += replacements

        remaining = max(0, MAX_CONTEXT_CHARS - self.used_chars)
        allowed = min(maximum, remaining)
        if len(result) > allowed:
            result = result[:allowed] + "\n[TRUNCATED]"
            self.truncated = True
        self.used_chars += len(result)
        return result

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value, maximum=10_000)
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                key_text = str(key)
                if _SECRET_KEY.fullmatch(key_text):
                    result[key_text] = "[REDACTED]"
                    self.redaction_count += 1
                else:
                    result[key_text] = self.value(item)
            return result
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value


def _parse_cursor_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def source_snapshot(session: Session, period_end_time: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source in SOURCES:
        cursors = session.scalars(
            select(SyncCursor)
            .where(SyncCursor.source == source)
            .order_by(SyncCursor.updated_at.desc())
        ).all()
        coverage_values = [
            parsed
            for item in cursors
            if (parsed := _parse_cursor_time((item.cursor or {}).get("until"))) is not None
        ]
        coverage = max(coverage_values) if coverage_values else None
        latest = cursors[0].updated_at if cursors else None
        fresh = coverage is not None and coverage >= period_end_time
        result[source] = {
            "status": "fresh" if fresh else ("stale" if coverage else "missing"),
            "coverage_through": (
                period_end_time.isoformat()
                if fresh
                else (coverage.isoformat() if coverage else None)
            ),
            "latest_sync_at": None if fresh else (latest.isoformat() if latest else None),
        }
    return result


def build_report_context(
    session: Session,
    cadence: ReportCadence,
    period: ParsedPeriod,
) -> dict[str, Any]:
    period_start, period_end, period_key = period_bounds(cadence, period)
    from_time, to_time = period_datetimes(cadence, period)
    redactor = ContextRedactor()

    event_models = session.scalars(
        select(ActivityEvent)
        .where(ActivityEvent.occurred_at >= from_time, ActivityEvent.occurred_at < to_time)
        .order_by(ActivityEvent.occurred_at.asc(), ActivityEvent.id.asc())
        .limit(MAX_EVENTS + 1)
    ).all()
    if len(event_models) > MAX_EVENTS:
        event_models = event_models[:MAX_EVENTS]
        redactor.truncated = True

    event_counts = Counter(item.source for item in event_models)
    artifact_ids = {item.artifact_id for item in event_models if item.artifact_id}
    artifact_models = []
    if artifact_ids:
        artifact_models = session.scalars(
            select(Artifact)
            .options(selectinload(Artifact.versions))
            .where(Artifact.id.in_(artifact_ids))
            .order_by(Artifact.source.asc(), Artifact.remote_id.asc())
        ).all()

    activities = []
    for event in event_models:
        activities.append(
            {
                "id": event.id,
                "source": event.source,
                "kind": event.kind,
                "action": event.action,
                "occurred_at": event.occurred_at,
                "actor_remote_id": event.actor_remote_id,
                "actor_is_self": event.actor_is_self,
                "artifact_remote_id": event.artifact_remote_id,
                "title": redactor.text(event.title, maximum=1_000) or "",
                "changes": redactor.value(event.changes),
                "url": event.url,
            }
        )

    artifacts = []
    for artifact in artifact_models:
        versions = []
        for version in artifact.versions:
            created = version.created_at_remote
            if created is not None:
                aware = created if created.tzinfo else created.replace(tzinfo=UTC)
                if not (from_time <= aware.astimezone(UTC) < to_time):
                    continue
            versions.append(
                {
                    "remote_version_id": version.remote_version_id,
                    "author_remote_id": version.author_remote_id,
                    "body_text": redactor.text(version.body_text),
                    "created_at_remote": version.created_at_remote,
                }
            )
        artifacts.append(
            {
                "source": artifact.source,
                "remote_id": artifact.remote_id,
                "kind": artifact.kind,
                "title": redactor.text(artifact.title, maximum=1_000) or "",
                "body_text": redactor.text(artifact.body_text),
                "state": artifact.state,
                "namespace": artifact.namespace,
                "url": artifact.url,
                "created_at_remote": artifact.created_at_remote,
                "updated_at_remote": artifact.updated_at_remote,
                "versions": versions,
            }
        )

    daily_documents = []
    if cadence in {"weekly", "monthly", "overall"}:
        daily_models = session.scalars(
            select(GeneratedReport)
            .where(
                GeneratedReport.cadence == "daily",
                GeneratedReport.period_start >= period_start,
                GeneratedReport.period_start < period_end,
            )
            .order_by(GeneratedReport.period_start.asc(), GeneratedReport.kind.asc())
        ).all()
        for report in daily_models:
            daily_documents.append(
                {
                    "period": report.period_start,
                    "kind": report.kind,
                    "status": report.status,
                    "title": redactor.text(report.title, maximum=500) or "",
                    "markdown": redactor.text(report.markdown, maximum=30_000) or "",
                    "source_snapshot": report.source_snapshot,
                }
            )

    snapshot = source_snapshot(session, to_time)
    existing_models = session.scalars(
        select(GeneratedReport).where(
            GeneratedReport.cadence == cadence,
            GeneratedReport.period_start == period_start,
        )
    ).all()
    existing = [
        {
            "kind": item.kind,
            "status": item.status,
            "current_revision": item.current_revision,
            "source_snapshot_hash": snapshot_hash(item.source_snapshot),
            "content_sha256": item.content_sha256,
            "updated_at": item.updated_at,
        }
        for item in existing_models
    ]
    all_fresh = all(item["status"] == "fresh" for item in snapshot.values())
    return {
        "cadence": cadence,
        "period": period_key,
        "period_start": period_start,
        "period_end": period_end,
        "from_time": from_time,
        "to_time": to_time,
        "activity_count": len(event_models),
        "source_event_counts": {source: event_counts.get(source, 0) for source in SOURCES},
        "activities": activities,
        "artifacts": artifacts,
        "daily_documents": daily_documents,
        "source_snapshot": snapshot,
        "source_snapshot_hash": snapshot_hash(snapshot),
        "all_sources_fresh": all_fresh,
        "redaction_count": redactor.redaction_count,
        "truncated": redactor.truncated,
        "existing": existing,
    }


def missing_report_periods(
    session: Session,
    cadence: ScheduledReportCadence,
    from_date: date,
    to_date: date,
    include_partial: bool,
) -> list[dict[str, Any]]:
    periods: list[tuple[date, str]] = []
    if cadence == "daily":
        cursor = from_date
        while cursor <= to_date:
            periods.append((cursor, cursor.isoformat()))
            cursor += timedelta(days=1)
    elif cadence == "weekly":
        cursor = from_date - timedelta(days=from_date.weekday())
        last = to_date - timedelta(days=to_date.weekday())
        while cursor <= last:
            iso_year, iso_week, _ = cursor.isocalendar()
            periods.append((cursor, f"{iso_year}-W{iso_week:02d}"))
            cursor += timedelta(days=7)
    else:
        cursor = from_date.replace(day=1)
        last = to_date.replace(day=1)
        while cursor <= last:
            periods.append((cursor, cursor.strftime("%Y-%m")))
            _, days = calendar.monthrange(cursor.year, cursor.month)
            cursor += timedelta(days=days)

    rows = session.scalars(
        select(GeneratedReport).where(
            GeneratedReport.cadence == cadence,
            GeneratedReport.period_start >= periods[0][0],
            GeneratedReport.period_start <= periods[-1][0],
        )
    ).all()
    indexed = {(row.period_start, row.kind): row for row in rows}
    result = []
    for start, key in periods:
        missing = [kind for kind in REPORT_KINDS if (start, kind) not in indexed]
        partial = [
            kind
            for kind in REPORT_KINDS
            if (start, kind) in indexed and indexed[(start, kind)].status == "partial"
        ]
        if include_partial and partial:
            _, period_end, _ = period_bounds(cadence, start)
            end_time = datetime.combine(period_end, datetime.min.time(), SEOUL).astimezone(UTC)
            current_snapshot = source_snapshot(session, end_time)
            partial = [
                kind
                for kind in partial
                if indexed[(start, kind)].source_snapshot != current_snapshot
            ]
        if missing or (include_partial and partial):
            result.append(
                {
                    "period": key,
                    "status": "missing" if missing else "partial",
                    "missing_kinds": missing,
                    "partial_kinds": partial,
                }
            )
    return result


def upsert_generated_report(
    session: Session,
    cadence: ReportCadence,
    period: ParsedPeriod,
    kind: ReportKind,
    payload: dict[str, Any],
) -> tuple[GeneratedReport, bool]:
    period_start, period_end, _ = period_bounds(cadence, period)
    content_sha256 = hashlib.sha256(payload["markdown"].encode()).hexdigest()
    report = session.scalar(
        select(GeneratedReport)
        .where(
            GeneratedReport.cadence == cadence,
            GeneratedReport.period_start == period_start,
            GeneratedReport.kind == kind,
        )
        .with_for_update()
    )
    changed = True
    if report is None:
        report = GeneratedReport(
            cadence=cadence,
            period_start=period_start,
            period_end=period_end,
            kind=kind,
            current_revision=1,
        )
        session.add(report)
    else:
        comparable = (
            report.status,
            report.title,
            report.content_sha256,
            report.source_snapshot,
            report.source_event_counts,
            report.prompt_version,
            report.generator_model,
        )
        incoming = (
            payload["status"],
            payload["title"],
            content_sha256,
            payload["source_snapshot"],
            payload["source_event_counts"],
            payload["prompt_version"],
            payload["generator_model"],
        )
        changed = comparable != incoming
        if changed:
            report.current_revision += 1

    if not changed:
        return report, False

    report.status = payload["status"]
    report.title = payload["title"]
    report.markdown = payload["markdown"]
    report.source_snapshot = payload["source_snapshot"]
    report.source_event_counts = payload["source_event_counts"]
    report.prompt_version = payload["prompt_version"]
    report.generator_model = payload["generator_model"]
    report.content_sha256 = content_sha256
    report.updated_at = utcnow()
    if report.status == "final":
        report.finalized_at = utcnow()
    else:
        report.finalized_at = None

    session.flush()

    session.add(
        GeneratedReportVersion(
            report_id=report.id,
            revision=report.current_revision,
            status=report.status,
            title=report.title,
            markdown=report.markdown,
            source_snapshot=report.source_snapshot,
            source_event_counts=report.source_event_counts,
            prompt_version=report.prompt_version,
            generator_model=report.generator_model,
            content_sha256=report.content_sha256,
        )
    )
    return report, True
