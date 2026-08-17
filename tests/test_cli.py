from __future__ import annotations

from datetime import UTC, datetime, timedelta

from work_history.cli import _advance_slack_coverage
from work_history.services import get_cursor


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
