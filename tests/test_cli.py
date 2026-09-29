from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from work_history import cli
from work_history.cli import _advance_slack_coverage, _sync_one, _tracked_run
from work_history.models import SyncRun
from work_history.schemas import NormalizedBatch
from work_history.services import get_cursor, set_cursor


def test_slack_coverage_advances_only_from_a_contiguous_seed(session_factory) -> None:
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = start + timedelta(days=7)
    with session_factory() as session:
        _advance_slack_coverage(session, start, end, None)
        assert get_cursor(session, "slack", "coverage") is None

        _advance_slack_coverage(session, start, end, start)
        session.commit()
        cursor = get_cursor(session, "slack", "coverage")
        assert cursor is not None
        assert cursor["since"] == start.isoformat()
        assert cursor["until"] == end.isoformat()

        gap_start = end + timedelta(days=1)
        _advance_slack_coverage(session, gap_start, gap_start + timedelta(days=1), None)
        assert get_cursor(session, "slack", "coverage")["until"] == end.isoformat()

        _advance_slack_coverage(session, end, end + timedelta(days=1), None)
        assert get_cursor(session, "slack", "coverage")["until"] == (
            end + timedelta(days=1)
        ).isoformat()


class _FakeJira:
    def __init__(self) -> None:
        self.known_updates: dict[str, str] = {}
        self.seen_updates = {"ABC-1": "2026-08-05T00:00:00.000+0000"}
        self.counters = {"failed_queries": 1, "skipped_unchanged": 2}
        self.closed = False

    def collect(self, start, end) -> NormalizedBatch:
        return NormalizedBatch()

    def close(self) -> None:
        self.closed = True


def test_incremental_sync_carries_jira_state_and_reports_partial(
    settings, session_factory, monkeypatch
) -> None:
    fake = _FakeJira()
    monkeypatch.setattr(cli, "_collector", lambda source, settings: fake)
    with session_factory() as session:
        set_cursor(session, "jira", "default", {"until": "x", "issues": {"OLD-1": "t"}})
        session.commit()
    end = datetime(2026, 8, 5, 1, tzinfo=UTC)

    result = _sync_one("jira", end - timedelta(hours=48), end, "incremental", settings,
                       session_factory)

    assert fake.known_updates == {"OLD-1": "t"}
    assert fake.closed
    assert result["status"] == "partial"
    with session_factory() as session:
        assert get_cursor(session, "jira", "default")["issues"] == fake.seen_updates
        run = session.scalar(select(SyncRun))
        assert run.status == "partial"
        assert run.counters["failed_queries"] == 1


def test_reconcile_does_not_skip_unchanged_issues(settings, session_factory, monkeypatch) -> None:
    fake = _FakeJira()
    monkeypatch.setattr(cli, "_collector", lambda source, settings: fake)
    with session_factory() as session:
        set_cursor(session, "jira", "default", {"until": "x", "issues": {"OLD-1": "t"}})
        session.commit()
    end = datetime(2026, 8, 5, 1, tzinfo=UTC)
    _sync_one("jira", end - timedelta(days=14), end, "reconcile", settings, session_factory)
    assert fake.known_updates == {}


def test_collector_setup_failure_marks_the_run_failed(settings, session_factory) -> None:
    end = datetime(2026, 8, 5, 1, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="Missing Slack settings"):
        _sync_one("slack", end - timedelta(days=1), end, "daily_reconcile", settings,
                  session_factory)
    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        assert run.status == "failed"
        assert "Missing Slack settings" in run.error


def test_tracked_run_records_success_and_failure(session_factory) -> None:
    with _tracked_run(session_factory, "raw_archive", "archive") as result:
        result["batches"] = 2
    with pytest.raises(KeyboardInterrupt):
        with _tracked_run(session_factory, "raw_archive", "archive_verify"):
            raise KeyboardInterrupt
    with session_factory() as session:
        runs = {run.job_kind: run for run in session.scalars(select(SyncRun))}
    assert runs["archive"].status == "success"
    assert runs["archive"].counters == {"batches": 2}
    assert runs["archive_verify"].status == "failed"


def test_record_failure_stores_the_systemd_result(tmp_path, monkeypatch) -> None:
    database = tmp_path / "status.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{database}")
    monkeypatch.setenv("MONITOR_SERVICE_RESULT", "exit-code")
    monkeypatch.setenv("MONITOR_EXIT_STATUS", "1")
    monkeypatch.setattr(sys, "argv", ["work-history", "db-init"])
    cli.main()
    monkeypatch.setattr(
        sys, "argv", ["work-history", "record-failure", "--unit", "work-history-sync.service"]
    )
    cli.main()

    from work_history.db import create_database_engine, create_session_factory

    factory = create_session_factory(create_database_engine(f"sqlite+pysqlite:///{database}"))
    with factory() as session:
        run = session.scalar(select(SyncRun))
    assert (run.source, run.job_kind, run.status) == (
        "systemd",
        "work-history-sync.service",
        "failed",
    )
    assert run.error == "service_result=exit-code exit_status=1"
