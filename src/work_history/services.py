from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from work_history.models import (
    ActivityEvent,
    Artifact,
    ArtifactVersion,
    IngestBatch,
    IngestNonce,
    RawRecord,
    SourceIdentity,
    SyncCursor,
    SyncRun,
    utcnow,
)
from work_history.schemas import GitLabIngestBatch, NormalizedBatch

_CONTENT_KEYS = {
    "body",
    "body_text",
    "comment",
    "content",
    "description",
    "description_html",
    "message",
    "renderedfields",
    "storage",
    "text",
}


def _metadata_only(value: Any) -> Any:
    """Remove source-authored content from indefinitely retained metadata."""
    if isinstance(value, dict):
        return {
            str(key): _metadata_only(item)
            for key, item in value.items()
            if str(key).lower() not in _CONTENT_KEYS
        }
    if isinstance(value, list):
        return [_metadata_only(item) for item in value]
    if isinstance(value, str) and len(value) > 2048:
        return value[:2048]
    return value


def _copy_fields(target: object, source: object, mapping: dict[str, str]) -> None:
    for source_name, target_name in mapping.items():
        setattr(target, target_name, getattr(source, source_name))


def upsert_normalized_batch(
    session: Session,
    batch: NormalizedBatch,
    raw_retention_days: int,
) -> dict[str, int]:
    counts = {"accepted": 0, "duplicates": 0}
    identities: dict[tuple[str, str], SourceIdentity] = {}
    versions: dict[tuple[str, str], ArtifactVersion] = {}
    events: dict[tuple[str, str], ActivityEvent] = {}
    raw_records: dict[tuple[str, str], RawRecord] = {}

    for record in batch.identities:
        identity_key = (record.source, record.remote_id)
        model = identities.get(identity_key)
        if model is None:
            model = session.scalar(
                select(SourceIdentity).where(
                    SourceIdentity.source == record.source,
                    SourceIdentity.remote_id == record.remote_id,
                )
            )
        if model is None:
            model = SourceIdentity(source=record.source, remote_id=record.remote_id)
            session.add(model)
            counts["accepted"] += 1
        else:
            counts["duplicates"] += 1
        identities[identity_key] = model
        _copy_fields(
            model,
            record,
            {
                "username": "username",
                "display_name": "display_name",
                "email": "email",
                "is_self": "is_self",
                "raw": "raw",
            },
        )
        model.raw = _metadata_only(record.raw)

    artifacts: dict[tuple[str, str], Artifact] = {}
    for record in batch.artifacts:
        artifact_key = (record.source, record.remote_id)
        model = artifacts.get(artifact_key)
        if model is None:
            model = session.scalar(
                select(Artifact).where(
                    Artifact.source == record.source,
                    Artifact.remote_id == record.remote_id,
                )
            )
        if model is None:
            model = Artifact(source=record.source, remote_id=record.remote_id, kind=record.kind)
            session.add(model)
            session.flush()
            counts["accepted"] += 1
        else:
            counts["duplicates"] += 1
        _copy_fields(
            model,
            record,
            {
                "kind": "kind",
                "title": "title",
                "body_text": "body_text",
                "state": "state",
                "namespace": "namespace",
                "url": "url",
                "created_at": "created_at_remote",
                "updated_at": "updated_at_remote",
                "raw": "raw",
            },
        )
        model.raw = _metadata_only(record.raw)
        model.last_seen_at = utcnow()
        model.unavailable_count = 0
        model.deleted_at = None
        artifacts[artifact_key] = model

    for record in batch.versions:
        artifact = artifacts.get((record.source, record.artifact_remote_id))
        if artifact is None:
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.source == record.source,
                    Artifact.remote_id == record.artifact_remote_id,
                )
            )
        if artifact is None:
            continue
        version_key = (artifact.id, record.remote_version_id)
        model = versions.get(version_key)
        if model is None:
            model = session.scalar(
                select(ArtifactVersion).where(
                    ArtifactVersion.artifact_id == artifact.id,
                    ArtifactVersion.remote_version_id == record.remote_version_id,
                )
            )
        if model is None:
            model = ArtifactVersion(
                artifact_id=artifact.id,
                remote_version_id=record.remote_version_id,
            )
            session.add(model)
            counts["accepted"] += 1
        else:
            counts["duplicates"] += 1
        versions[version_key] = model
        _copy_fields(
            model,
            record,
            {
                "author_remote_id": "author_remote_id",
                "body_text": "body_text",
                "created_at": "created_at_remote",
                "raw": "raw",
            },
        )
        model.raw = _metadata_only(record.raw)

    for record in batch.events:
        artifact = None
        if record.artifact_remote_id:
            artifact = artifacts.get((record.source, record.artifact_remote_id))
            if artifact is None:
                artifact = session.scalar(
                    select(Artifact).where(
                        Artifact.source == record.source,
                        Artifact.remote_id == record.artifact_remote_id,
                    )
                )
        event_key = (record.source, record.event_key)
        model = events.get(event_key)
        if model is None:
            model = session.scalar(
                select(ActivityEvent).where(
                    ActivityEvent.source == record.source,
                    ActivityEvent.event_key == record.event_key,
                )
            )
        if model is None:
            model = ActivityEvent(
                source=record.source,
                event_key=record.event_key,
                kind=record.kind,
                action=record.action,
                occurred_at=record.occurred_at,
            )
            session.add(model)
            counts["accepted"] += 1
        else:
            counts["duplicates"] += 1
        events[event_key] = model
        _copy_fields(
            model,
            record,
            {
                "kind": "kind",
                "action": "action",
                "occurred_at": "occurred_at",
                "actor_remote_id": "actor_remote_id",
                "actor_is_self": "actor_is_self",
                "artifact_remote_id": "artifact_remote_id",
                "title": "title",
                "changes": "changes",
                "url": "url",
                "raw": "raw",
            },
        )
        model.raw = _metadata_only(record.raw)
        model.artifact_id = artifact.id if artifact else None

    retention = timedelta(days=raw_retention_days)
    for record in batch.raw_records:
        raw_key = (record.source, record.record_key)
        model = raw_records.get(raw_key)
        if model is None:
            model = session.scalar(
                select(RawRecord).where(
                    RawRecord.source == record.source,
                    RawRecord.record_key == record.record_key,
                )
            )
        if model is None:
            model = RawRecord(
                source=record.source,
                record_key=record.record_key,
                kind=record.kind,
                payload=record.payload,
                collected_at=record.collected_at,
                expires_at=record.collected_at + retention,
            )
            session.add(model)
            counts["accepted"] += 1
        else:
            model.payload = record.payload
            model.collected_at = record.collected_at
            model.expires_at = record.collected_at + retention
            counts["duplicates"] += 1
        raw_records[raw_key] = model

    for record in batch.unavailable_artifacts:
        model = session.scalar(
            select(Artifact).where(
                Artifact.source == record.source,
                Artifact.remote_id == record.remote_id,
            )
        )
        if model is None:
            continue
        model.unavailable_count += 1
        if model.unavailable_count >= 3:
            model.body_text = None
            model.raw = {}
            model.deleted_at = record.observed_at

    return counts


def get_cursor(session: Session, source: str, stream: str) -> dict[str, Any] | None:
    model = session.scalar(
        select(SyncCursor).where(
            SyncCursor.source == source,
            SyncCursor.stream == stream,
        )
    )
    return model.cursor if model else None


def set_cursor(
    session: Session,
    source: str,
    stream: str,
    cursor: dict[str, Any],
) -> None:
    model = session.scalar(
        select(SyncCursor).where(
            SyncCursor.source == source,
            SyncCursor.stream == stream,
        )
    )
    if model is None:
        model = SyncCursor(source=source, stream=stream, cursor=cursor)
        session.add(model)
    else:
        model.cursor = cursor
        model.updated_at = utcnow()


def ingest_gitlab_batch(
    session: Session,
    batch: GitLabIngestBatch,
    device_id: str,
    body_hash: str,
    raw_retention_days: int,
) -> dict[str, Any]:
    existing = session.get(IngestBatch, batch.batch_id)
    if existing:
        if existing.body_hash != body_hash or existing.device_id != device_id:
            raise ValueError("batch_id was already used with different content")
        return {
            "batch_id": batch.batch_id,
            "accepted": 0,
            "duplicates": existing.record_count,
            "duplicate_batch": True,
            "checkpoint_updated": bool(existing.next_checkpoint),
        }

    counts = upsert_normalized_batch(session, batch, raw_retention_days)
    checkpoint_updated = batch.next_checkpoint is not None
    if batch.next_checkpoint is not None:
        set_cursor(session, "gitlab", device_id, batch.next_checkpoint)
    session.add(
        IngestBatch(
            batch_id=batch.batch_id,
            device_id=device_id,
            schema_version=batch.schema_version,
            body_hash=body_hash,
            record_count=batch.record_count,
            next_checkpoint=batch.next_checkpoint,
        )
    )
    return {
        "batch_id": batch.batch_id,
        **counts,
        "duplicate_batch": False,
        "checkpoint_updated": checkpoint_updated,
    }


def remember_nonce(
    session: Session,
    device_id: str,
    nonce: str,
    ttl_seconds: int,
) -> bool:
    existing = session.scalar(
        select(IngestNonce).where(
            IngestNonce.device_id == device_id,
            IngestNonce.nonce == nonce,
        )
    )
    if existing:
        return False
    now = utcnow()
    session.add(
        IngestNonce(
            device_id=device_id,
            nonce=nonce,
            seen_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds * 2),
        )
    )
    return True


def cleanup_expired(session: Session) -> dict[str, int]:
    from work_history.raw_archive import RawSnapshot, verified_fingerprints

    now = utcnow()
    expired = list(session.scalars(select(RawRecord).where(RawRecord.expires_at < now)))
    snapshots = [RawSnapshot.from_model(record) for record in expired]
    verified = verified_fingerprints(session, snapshots)
    archived_raw_records = 0
    retained_raw_records = 0
    for record, snapshot in zip(expired, snapshots, strict=True):
        if snapshot.fingerprint in verified:
            session.delete(record)
            archived_raw_records += 1
        else:
            retained_raw_records += 1
    nonce_result = session.execute(delete(IngestNonce).where(IngestNonce.expires_at < now))
    return {
        "raw_records": archived_raw_records,
        "unarchived_raw_records": retained_raw_records,
        "nonces": nonce_result.rowcount or 0,
    }


def start_sync_run(session: Session, source: str, job_kind: str) -> SyncRun:
    run = SyncRun(source=source, job_kind=job_kind, status="running")
    session.add(run)
    session.flush()
    return run


def finish_sync_run(
    run: SyncRun,
    status: str,
    counters: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.counters = counters or {}
    run.error = error
