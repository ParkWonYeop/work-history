from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class SourceIdentity(Base):
    __tablename__ = "source_identities"
    __table_args__ = (UniqueConstraint("source", "remote_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_id: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("source", "remote_id"),
        Index("ix_artifacts_source_updated", "source", "updated_at_remote"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_text: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(128))
    namespace: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(Text)
    created_at_remote: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_remote: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    unavailable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    versions: Mapped[list[ArtifactVersion]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )
    events: Mapped[list[ActivityEvent]] = relationship(back_populates="artifact")


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (UniqueConstraint("artifact_id", "remote_version_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remote_version_id: Mapped[str] = mapped_column(String(512), nullable=False)
    author_remote_id: Mapped[str | None] = mapped_column(String(512))
    body_text: Mapped[str | None] = mapped_column(Text)
    created_at_remote: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    artifact: Mapped[Artifact] = relationship(back_populates="versions")


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        UniqueConstraint("source", "event_key"),
        Index("ix_activity_occurred_id", "occurred_at", "id"),
        Index("ix_activity_source_occurred", "source", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_key: Mapped[str] = mapped_column(String(768), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_remote_id: Mapped[str | None] = mapped_column(String(512))
    actor_is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    artifact_remote_id: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    artifact: Mapped[Artifact | None] = relationship(back_populates="events")


class RawRecord(Base):
    __tablename__ = "raw_records"
    __table_args__ = (
        UniqueConstraint("source", "record_key"),
        Index("ix_raw_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    record_key: Mapped[str] = mapped_column(String(768), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (UniqueConstraint("source", "stream"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    stream: Mapped[str] = mapped_column(String(255), nullable=False)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class IngestDevice(Base):
    __tablename__ = "ingest_devices"

    device_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    public_key_b64: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, default="gitlab_ingest", server_default="gitlab_ingest"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestNonce(Base):
    __tablename__ = "ingest_nonces"
    __table_args__ = (UniqueConstraint("device_id", "nonce"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(255), nullable=False)
    nonce: Mapped[str] = mapped_column(String(255), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IngestBatch(Base):
    __tablename__ = "ingest_batches"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    next_checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class GeneratedReport(Base):
    __tablename__ = "generated_reports"
    __table_args__ = (
        UniqueConstraint("cadence", "period_start", "kind"),
        Index("ix_generated_reports_period", "cadence", "period_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    cadence: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_event_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    versions: Mapped[list[GeneratedReportVersion]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="GeneratedReportVersion.revision",
    )


class GeneratedReportVersion(Base):
    __tablename__ = "generated_report_versions"
    __table_args__ = (
        UniqueConstraint("report_id", "revision"),
        Index("ix_generated_report_versions_report", "report_id", "revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("generated_reports.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_event_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    report: Mapped[GeneratedReport] = relationship(back_populates="versions")
