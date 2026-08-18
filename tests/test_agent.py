from __future__ import annotations

from datetime import UTC, datetime, timedelta

from work_history.gitlab_agent import _choose_window, _scheduled_run_due, _split_batch
from work_history.schemas import ActivityRecord, NormalizedBatch


def test_agent_splits_large_payload_and_only_final_batch_advances_checkpoint() -> None:
    now = datetime.now(UTC)
    normalized = NormalizedBatch(
        events=[
            ActivityRecord(
                source="gitlab",
                event_key=f"event:{index}",
                kind="event",
                action="created",
                occurred_at=now,
            )
            for index in range(901)
        ]
    )
    checkpoint = {"until": now.isoformat()}
    batches = _split_batch(
        normalized,
        "work-mac",
        "https://gitlab.internal",
        checkpoint,
    )

    assert [batch.record_count for batch in batches] == [400, 400, 101]
    assert all(batch.next_checkpoint is None for batch in batches[:-1])
    assert batches[-1].next_checkpoint == checkpoint
    assert all(batch.record_count <= 500 for batch in batches)


def test_empty_windows_have_checkpoint_specific_idempotency_keys() -> None:
    normalized = NormalizedBatch()
    first_checkpoint = {"until": "2026-04-08T00:00:00+00:00"}
    second_checkpoint = {"until": "2026-04-15T00:00:00+00:00"}

    first = _split_batch(
        normalized,
        "work-mac",
        "https://gitlab.internal",
        first_checkpoint,
    )[0]
    retry = _split_batch(
        normalized,
        "work-mac",
        "https://gitlab.internal",
        first_checkpoint,
    )[0]
    second = _split_batch(
        normalized,
        "work-mac",
        "https://gitlab.internal",
        second_checkpoint,
    )[0]

    assert first.batch_id == retry.batch_id
    assert first.batch_id != second.batch_id
    assert first.next_checkpoint == first_checkpoint
    assert second.next_checkpoint == second_checkpoint


def test_replay_batches_never_advance_checkpoint_and_are_window_scoped() -> None:
    normalized = NormalizedBatch()
    first = _split_batch(
        normalized,
        "work-mac",
        "https://gitlab.internal",
        None,
        batch_scope="replay:2026-04-01:2026-04-08",
    )[0]
    retry = _split_batch(
        normalized,
        "work-mac",
        "https://gitlab.internal",
        None,
        batch_scope="replay:2026-04-01:2026-04-08",
    )[0]
    following = _split_batch(
        normalized,
        "work-mac",
        "https://gitlab.internal",
        None,
        batch_scope="replay:2026-04-08:2026-04-15",
    )[0]

    assert first.batch_id == retry.batch_id
    assert first.batch_id != following.batch_id
    assert first.next_checkpoint is None
    assert following.next_checkpoint is None


def test_scheduled_run_retries_until_success_and_catches_up_backlog() -> None:
    monday_morning = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)  # 09:00 Asia/Seoul
    yesterday = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    today = datetime(2026, 8, 3, 0, 5, tzinfo=UTC)

    assert _scheduled_run_due(None, monday_morning)
    assert _scheduled_run_due(
        {"until": monday_morning.isoformat(), "last_success": yesterday.isoformat()},
        monday_morning,
    )
    assert not _scheduled_run_due(
        {"until": monday_morning.isoformat(), "last_success": today.isoformat()},
        monday_morning,
    )
    assert _scheduled_run_due(
        {
            "until": (monday_morning - timedelta(days=3)).isoformat(),
            "last_success": today.isoformat(),
        },
        monday_morning,
    )


def test_scheduled_run_skips_weekends_and_outside_retry_hours() -> None:
    saturday_morning = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    monday_before_window = datetime(2026, 8, 2, 23, 59, tzinfo=UTC)
    monday_after_window = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

    assert not _scheduled_run_due(None, saturday_morning)
    assert not _scheduled_run_due(None, monday_before_window)
    assert not _scheduled_run_due(None, monday_after_window)


def test_initial_window_starts_at_configured_employment_date() -> None:
    employment_date = datetime(2026, 3, 31, 15, 0, tzinfo=UTC)
    now = employment_date + timedelta(days=100)
    start, end = _choose_window(None, employment_date, now)

    assert start == employment_date
    assert end == employment_date + timedelta(days=7)


def test_windows_advance_without_gaps_until_target_time() -> None:
    target = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
    checkpoint: dict[str, str] | None = {
        "until": "2026-04-28T15:00:00+00:00"
    }
    windows: list[tuple[datetime, datetime]] = []

    while True:
        start, end = _choose_window(checkpoint, now=target)
        windows.append((start, end))
        checkpoint = {"until": end.isoformat()}
        if end >= target:
            break

    assert windows[0][0] == datetime(2026, 4, 28, 15, 0, tzinfo=UTC)
    assert windows[-1][1] == target
    assert len(windows) == 15
    assert all(
        current[1] >= following[0]
        for current, following in zip(windows, windows[1:], strict=False)
    )
