from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from work_history.models import ActivityEvent, Artifact
from work_history.schemas import (
    ActivityRecord,
    ArtifactRecord,
    ArtifactUnavailableRecord,
    NormalizedBatch,
)
from work_history.services import upsert_normalized_batch


def test_upsert_is_idempotent_even_with_duplicates_in_one_batch(session_factory) -> None:
    now = datetime.now(UTC)
    artifact = ArtifactRecord(
        source="gitlab",
        remote_id="project:1:commit:abc",
        kind="commit",
        title="A commit",
        created_at=now,
        updated_at=now,
    )
    event = ActivityRecord(
        source="gitlab",
        event_key="commit:1:abc",
        kind="commit",
        action="committed",
        occurred_at=now,
        artifact_remote_id=artifact.remote_id,
        title="A commit",
    )
    batch = NormalizedBatch(
        artifacts=[artifact, artifact.model_copy()],
        events=[event, event.model_copy()],
    )

    with session_factory() as session:
        upsert_normalized_batch(session, batch, 30)
        session.commit()
    with session_factory() as session:
        upsert_normalized_batch(session, batch, 30)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Artifact)) == 1
        assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 1
        stored = session.scalar(select(ActivityEvent))
        assert stored.artifact_id is not None


def test_unavailable_artifact_content_is_purged_after_three_observations(
    session_factory,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        upsert_normalized_batch(
            session,
            NormalizedBatch(
                artifacts=[
                    ArtifactRecord(
                        source="jira",
                        remote_id="ABC-1",
                        kind="issue",
                        title="Sensitive issue",
                        body_text="sensitive body",
                        raw={"description": "sensitive body", "id": "1"},
                    )
                ]
            ),
            30,
        )
        session.commit()
    unavailable = NormalizedBatch(
        unavailable_artifacts=[
            ArtifactUnavailableRecord(source="jira", remote_id="ABC-1", observed_at=now)
        ]
    )
    for _ in range(3):
        with session_factory() as session:
            upsert_normalized_batch(session, unavailable, 30)
            session.commit()
    with session_factory() as session:
        artifact = session.scalar(select(Artifact))
        assert artifact.body_text is None
        assert artifact.deleted_at is not None
        assert "description" not in artifact.raw
