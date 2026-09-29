from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from work_history.api import create_app
from work_history.collectors import ConfluenceCollector, JiraCollector, SlackCollector
from work_history.config import Settings
from work_history.db import create_database_engine, create_session_factory
from work_history.models import Base, IngestDevice, SyncRun, utcnow
from work_history.security import b64url_decode
from work_history.services import (
    cleanup_expired,
    finish_sync_run,
    get_cursor,
    set_cursor,
    start_sync_run,
    upsert_normalized_batch,
)

logger = logging.getLogger(__name__)


def _parse_time(value: str, timezone_name: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(UTC)


@contextmanager
def _sync_lock() -> Iterator[None]:
    path = Path(os.getenv("SYNC_LOCK_FILE", "/tmp/work-history-sync.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another synchronization is already running") from exc
        yield


@contextmanager
def _archive_lock() -> Iterator[None]:
    path = Path(os.getenv("ARCHIVE_LOCK_FILE", "/tmp/work-history-archive.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another raw archive operation is already running") from exc
        yield


@contextmanager
def _tracked_run(session_factory, source: str, job_kind: str) -> Iterator[dict[str, Any]]:
    """Record a job in sync_runs so the status APIs can report its last success."""
    with session_factory() as session:
        run_id = start_sync_run(session, source, job_kind).id
        session.commit()
    result: dict[str, Any] = {}
    status, error = "success", None
    try:
        yield result
    except BaseException as exc:
        status, error = "failed", str(exc)[:4000] or type(exc).__name__
        raise
    finally:
        with session_factory() as session:
            run = session.get(SyncRun, run_id)
            if run:
                finish_sync_run(run, status, result, error)
                session.commit()


def _collector(source: str, settings: Settings):
    if source == "slack":
        settings.require_slack()
        return SlackCollector(settings.slack_workspace_url, settings.slack_user_token)
    settings.require_atlassian()
    cls = JiraCollector if source == "jira" else ConfluenceCollector
    return cls(
        settings.atlassian_site_url,
        settings.atlassian_email,
        settings.atlassian_api_token,
    )


def _advance_slack_coverage(
    session,
    start: datetime,
    end: datetime,
    seed_from: datetime | None,
) -> None:
    current = get_cursor(session, "slack", "coverage")
    if current is None:
        if seed_from is None:
            return
        set_cursor(
            session,
            "slack",
            "coverage",
            {"since": seed_from.isoformat(), "until": end.isoformat()},
        )
        return
    current_until = _parse_time(str(current.get("until")), "UTC")
    current_since = _parse_time(str(current.get("since")), "UTC")
    same_backfill = seed_from is not None and current_since == seed_from
    if current_until < start and not same_backfill:
        return
    set_cursor(
        session,
        "slack",
        "coverage",
        {
            "since": current_since.isoformat(),
            "until": max(current_until, end).isoformat(),
            "last_success": utcnow().isoformat(),
        },
    )


def _sync_one(
    source: str,
    start: datetime,
    end: datetime,
    job_kind: str,
    settings: Settings,
    session_factory,
    cursor_stream: str = "default",
    advance_slack_coverage: bool = False,
    slack_coverage_seed: datetime | None = None,
) -> dict[str, Any]:
    with session_factory() as session:
        run_id = start_sync_run(session, source, job_kind).id
        previous = get_cursor(session, source, cursor_stream) or {}
        session.commit()
    collector = None
    try:
        collector = _collector(source, settings)
        # Skipping unchanged Jira issues is only sound along the contiguous incremental chain.
        if job_kind == "incremental" and cursor_stream == "default" and hasattr(
            collector, "known_updates"
        ):
            collector.known_updates = previous.get("issues") or {}
        batch = collector.collect(start, end)
        extra = dict(getattr(collector, "counters", {}))
        status = "partial" if extra.get("failed_queries") else "success"
        with session_factory() as session:
            counts = upsert_normalized_batch(
                session,
                batch,
                settings.raw_retention_days,
            )
            cursor: dict[str, Any] = {
                "until": end.isoformat(),
                "last_success": utcnow().isoformat(),
            }
            if cursor_stream == "default" and hasattr(collector, "seen_updates"):
                cursor["issues"] = collector.seen_updates
            set_cursor(session, source, cursor_stream, cursor)
            if source == "slack" and advance_slack_coverage:
                _advance_slack_coverage(session, start, end, slack_coverage_seed)
            counters = {**counts, "records": batch.record_count, **extra}
            finish_sync_run(session.get(SyncRun, run_id), status, counters)
            session.commit()
        return {"status": status, **counters}
    except Exception as exc:
        with session_factory() as session:
            run = session.get(SyncRun, run_id)
            if run:
                finish_sync_run(run, "failed", error=str(exc)[:4000])
                session.commit()
        raise
    finally:
        if collector is not None:
            collector.close()


def command_sync(args: argparse.Namespace, settings: Settings, session_factory) -> int:
    sources = ["jira", "confluence"] if args.source == "all" else [args.source]
    now = datetime.now(UTC)
    failures = 0
    with _sync_lock():
        for source in sources:
            if args.from_time:
                start = _parse_time(args.from_time, settings.default_timezone)
            elif args.mode == "reconcile":
                start = now - timedelta(days=args.lookback_days)
            else:
                with session_factory() as session:
                    cursor = get_cursor(session, source, "default")
                if cursor and cursor.get("until"):
                    start = _parse_time(cursor["until"], settings.default_timezone) - timedelta(
                        hours=48
                    )
                else:
                    start = now - timedelta(hours=48)
            end = _parse_time(args.to_time, settings.default_timezone) if args.to_time else now
            try:
                counts = _sync_one(
                    source,
                    start,
                    end,
                    args.mode,
                    settings,
                    session_factory,
                )
                print(json.dumps({"source": source, "status": "success", **counts}))
            except Exception as exc:
                failures += 1
                logger.exception("%s synchronization failed", source)
                print(json.dumps({"source": source, "status": "failed", "error": str(exc)}))
    return 1 if failures else 0


def command_backfill(args: argparse.Namespace, settings: Settings, session_factory) -> int:
    sources = ["jira", "confluence"] if args.source == "all" else [args.source]
    requested_start = _parse_time(args.from_time, settings.default_timezone)
    requested_end = _parse_time(args.to_time, settings.default_timezone)
    if requested_end <= requested_start:
        raise RuntimeError("backfill end must be after start")
    with _sync_lock():
        for source in sources:
            stream = f"backfill:{args.from_time}:{args.to_time}"
            with session_factory() as session:
                cursor = get_cursor(session, source, stream)
            start = requested_start
            if cursor and cursor.get("until"):
                start = max(start, _parse_time(cursor["until"], settings.default_timezone))
            while start < requested_end:
                end = min(start + timedelta(days=args.chunk_days), requested_end)
                counts = _sync_one(
                    source,
                    start,
                    end,
                    "backfill",
                    settings,
                    session_factory,
                    cursor_stream=stream,
                    advance_slack_coverage=source == "slack",
                    slack_coverage_seed=requested_start if source == "slack" else None,
                )
                print(
                    json.dumps(
                        {
                            "source": source,
                            "from": start.isoformat(),
                            "to": end.isoformat(),
                            **counts,
                        }
                    )
                )
                start = end
    return 0


def command_slack_daily(args: argparse.Namespace, settings: Settings, session_factory) -> int:
    timezone = ZoneInfo(settings.default_timezone)
    target = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(timezone).date() - timedelta(days=1)
    )
    start = datetime.combine(target, time.min, timezone).astimezone(UTC)
    end = datetime.combine(target + timedelta(days=1), time.min, timezone).astimezone(UTC)
    stream = f"daily:{target.isoformat()}"
    with _sync_lock():
        with session_factory() as session:
            cursor = get_cursor(session, "slack", stream)
        if cursor and cursor.get("last_success") and not args.force:
            print(json.dumps({"source": "slack", "date": target.isoformat(), "status": "skipped"}))
            return 0
        counts = _sync_one(
            "slack",
            start,
            end,
            "daily_reconcile",
            settings,
            session_factory,
            cursor_stream=stream,
            advance_slack_coverage=True,
        )
    print(
        json.dumps(
            {
                "source": "slack",
                "date": target.isoformat(),
                "status": "success",
                **counts,
            }
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Work history server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("db-init")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--forwarded-allow-ips",
        default=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    register = sub.add_parser("register-device")
    register.add_argument("--device-id", required=True)
    register.add_argument("--public-key", required=True)
    register.add_argument(
        "--purpose",
        choices=["gitlab_ingest", "report_agent"],
        default="gitlab_ingest",
    )
    revoke = sub.add_parser("revoke-device")
    revoke.add_argument("--device-id", required=True)
    sync = sub.add_parser("sync")
    sync.add_argument(
        "--source",
        choices=["jira", "confluence", "slack", "all"],
        default="all",
    )
    sync.add_argument("--mode", choices=["incremental", "reconcile"], default="incremental")
    sync.add_argument("--lookback-days", type=int, default=14)
    sync.add_argument("--from", dest="from_time")
    sync.add_argument("--to", dest="to_time")
    backfill = sub.add_parser("backfill")
    backfill.add_argument(
        "--source",
        choices=["jira", "confluence", "slack", "all"],
        default="all",
    )
    backfill.add_argument("--from", dest="from_time", required=True)
    backfill.add_argument("--to", dest="to_time", required=True)
    backfill.add_argument("--chunk-days", type=int, default=7)
    slack_daily = sub.add_parser("slack-daily")
    slack_daily.add_argument("--date", help="Asia/Seoul date in YYYY-MM-DD; default is yesterday")
    slack_daily.add_argument("--force", action="store_true")
    sub.add_parser("slack-socket")
    archive = sub.add_parser("archive")
    archive.add_argument(
        "--all",
        action="store_true",
        help="archive every current raw record without changing its expiry",
    )
    archive.add_argument("--max-batches", type=int)
    archive_verify = sub.add_parser("archive-verify")
    archive_verify.add_argument("--local-only", action="store_true")
    sub.add_parser("archive-check")
    sub.add_parser("archive-list")
    archive_fetch = sub.add_parser("archive-fetch")
    archive_fetch.add_argument("--batch-id", required=True)
    archive_fetch.add_argument("--output", type=Path, required=True)
    backup_offsite = sub.add_parser("backup-offsite")
    backup_offsite.add_argument("path", type=Path)
    record_failure = sub.add_parser("record-failure")
    record_failure.add_argument("--unit", required=True)
    sub.add_parser("cleanup")
    return parser


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    settings = Settings.from_env()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    if args.command == "db-init":
        Base.metadata.create_all(engine)
        return
    if args.command == "serve":
        uvicorn.run(
            create_app(settings, session_factory),
            host=args.host,
            port=args.port,
            proxy_headers=True,
            forwarded_allow_ips=args.forwarded_allow_ips,
        )
        return
    if args.command == "register-device":
        raw = b64url_decode(args.public_key)
        Ed25519PublicKey.from_public_bytes(raw)
        with session_factory() as session:
            device = session.get(IngestDevice, args.device_id)
            if device is None:
                device = IngestDevice(
                    device_id=args.device_id,
                    public_key_b64=args.public_key,
                    purpose=args.purpose,
                )
                session.add(device)
            else:
                device.public_key_b64 = args.public_key
                device.purpose = args.purpose
                device.enabled = True
                device.revoked_at = None
            session.commit()
        return
    if args.command == "revoke-device":
        with session_factory() as session:
            device = session.get(IngestDevice, args.device_id)
            if not device:
                raise RuntimeError("device not found")
            device.enabled = False
            device.revoked_at = utcnow()
            session.commit()
        return
    if args.command == "sync":
        raise SystemExit(command_sync(args, settings, session_factory))
    if args.command == "backfill":
        raise SystemExit(command_backfill(args, settings, session_factory))
    if args.command == "slack-daily":
        raise SystemExit(command_slack_daily(args, settings, session_factory))
    if args.command == "slack-socket":
        from work_history.slack_socket import run_slack_socket

        run_slack_socket(settings, session_factory)
        return
    if args.command in {"archive", "archive-verify", "archive-check", "archive-fetch"}:
        from work_history.raw_archive import R2Store, RawArchiveManager

        settings.require_raw_archive()
        store = R2Store(settings)
        manager = RawArchiveManager(settings, session_factory, store)
        with _archive_lock():
            if args.command == "archive":
                job_kind = "archive_all" if args.all else "archive"
                with _tracked_run(session_factory, "raw_archive", job_kind) as result:
                    result.update(
                        manager.archive_pending(
                            include_current=args.all,
                            max_batches=args.max_batches,
                        )
                    )
                    result["r2"] = manager.inventory()
            elif args.command == "archive-verify":
                job_kind = "archive_verify_local" if args.local_only else "archive_verify"
                with _tracked_run(session_factory, "raw_archive", job_kind) as result:
                    result.update(manager.verify_all(remote=not args.local_only))
            elif args.command == "archive-check":
                result = store.check()
            else:
                result = manager.fetch(args.batch_id, args.output.resolve())
        print(json.dumps(result))
        return
    if args.command == "archive-list":
        from work_history.raw_archive import list_archive_batches

        with session_factory() as session:
            result = list_archive_batches(session)
        print(json.dumps(result))
        return
    if args.command == "backup-offsite":
        from work_history.raw_archive import R2Store, upload_db_backup

        with _tracked_run(session_factory, "backup", "offsite") as result:
            settings.require_raw_archive()
            result.update(upload_db_backup(settings, R2Store(settings), args.path))
        print(json.dumps(result))
        return
    if args.command == "record-failure":
        details = [
            f"{name.removeprefix('MONITOR_').lower()}={os.environ[name]}"
            for name in ("MONITOR_SERVICE_RESULT", "MONITOR_EXIT_CODE", "MONITOR_EXIT_STATUS")
            if os.getenv(name)
        ]
        with session_factory() as session:
            run = start_sync_run(session, "systemd", args.unit[:64])
            finish_sync_run(run, "failed", error=" ".join(details) or "unit failed")
            session.commit()
        return
    if args.command == "cleanup":
        with session_factory() as session:
            result = cleanup_expired(session)
        print(json.dumps(result))
        return


if __name__ == "__main__":
    main()
