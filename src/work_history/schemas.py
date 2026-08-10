from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


class IdentityRecord(BaseModel):
    source: Literal["jira", "confluence", "gitlab"]
    remote_id: str = Field(min_length=1, max_length=512)
    username: str | None = None
    display_name: str | None = None
    email: str | None = None
    is_self: bool = True
    raw: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(BaseModel):
    source: Literal["jira", "confluence", "gitlab"]
    remote_id: str = Field(min_length=1, max_length=512)
    kind: str = Field(min_length=1, max_length=64)
    title: str = ""
    body_text: str | None = None
    state: str | None = None
    namespace: str | None = None
    url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    _created_aware = field_validator("created_at")(_aware)
    _updated_aware = field_validator("updated_at")(_aware)


class ArtifactVersionRecord(BaseModel):
    source: Literal["jira", "confluence", "gitlab"]
    artifact_remote_id: str = Field(min_length=1, max_length=512)
    remote_version_id: str = Field(min_length=1, max_length=512)
    author_remote_id: str | None = None
    body_text: str | None = None
    created_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    _created_aware = field_validator("created_at")(_aware)


class ActivityRecord(BaseModel):
    source: Literal["jira", "confluence", "gitlab"]
    event_key: str = Field(min_length=1, max_length=768)
    kind: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    actor_remote_id: str | None = None
    actor_is_self: bool = True
    artifact_remote_id: str | None = None
    title: str = ""
    changes: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    _occurred_aware = field_validator("occurred_at")(_aware)


class RawRecordInput(BaseModel):
    source: Literal["jira", "confluence", "gitlab"]
    record_key: str = Field(min_length=1, max_length=768)
    kind: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]
    collected_at: datetime

    _collected_aware = field_validator("collected_at")(_aware)


class ArtifactUnavailableRecord(BaseModel):
    source: Literal["jira", "confluence", "gitlab"]
    remote_id: str = Field(min_length=1, max_length=512)
    observed_at: datetime

    _observed_aware = field_validator("observed_at")(_aware)


class NormalizedBatch(BaseModel):
    identities: list[IdentityRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    versions: list[ArtifactVersionRecord] = Field(default_factory=list)
    events: list[ActivityRecord] = Field(default_factory=list)
    raw_records: list[RawRecordInput] = Field(default_factory=list)
    unavailable_artifacts: list[ArtifactUnavailableRecord] = Field(default_factory=list)

    @property
    def record_count(self) -> int:
        return sum(
            len(items)
            for items in (
                self.identities,
                self.artifacts,
                self.versions,
                self.events,
                self.raw_records,
                self.unavailable_artifacts,
            )
        )


class GitLabIngestBatch(NormalizedBatch):
    schema_version: Literal[1] = 1
    batch_id: str = Field(min_length=16, max_length=64)
    source_instance: str = Field(min_length=1, max_length=512)
    collected_at: datetime
    next_checkpoint: dict[str, Any] | None = None

    _collected_aware = field_validator("collected_at")(_aware)

    @model_validator(mode="after")
    def validate_size(self) -> GitLabIngestBatch:
        if self.record_count > 500:
            raise ValueError("a batch may contain at most 500 records")
        for collection in (
            self.identities,
            self.artifacts,
            self.versions,
            self.events,
            self.raw_records,
            self.unavailable_artifacts,
        ):
            if any(record.source != "gitlab" for record in collection):
                raise ValueError("GitLab ingestion accepts only source=gitlab records")
        return self


class IngestResponse(BaseModel):
    batch_id: str
    accepted: int
    duplicates: int
    duplicate_batch: bool = False
    checkpoint_updated: bool = False


class ActivityItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    kind: str
    action: str
    occurred_at: datetime
    actor_remote_id: str | None
    actor_is_self: bool
    artifact_remote_id: str | None
    title: str
    changes: dict[str, Any]
    url: str | None


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    next_cursor: str | None


class ArtifactVersionItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    remote_version_id: str
    author_remote_id: str | None
    body_text: str | None
    created_at_remote: datetime | None


class ArtifactItem(BaseModel):
    id: str
    source: str
    remote_id: str
    kind: str
    title: str
    body_text: str | None
    state: str | None
    namespace: str | None
    url: str | None
    created_at_remote: datetime | None
    updated_at_remote: datetime | None
    versions: list[ArtifactVersionItem]


class CheckpointResponse(BaseModel):
    device_id: str
    checkpoint: dict[str, Any] | None


ReportCadence = Literal["daily", "weekly", "monthly", "overall"]
ScheduledReportCadence = Literal["daily", "weekly", "monthly"]
ReportKind = Literal["work_report", "feedback"]
ReportStatus = Literal["partial", "final"]


class ReportContextRequest(BaseModel):
    cadence: ReportCadence
    period: str = Field(min_length=7, max_length=32)


class ReportMissingRequest(BaseModel):
    cadence: ScheduledReportCadence
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")
    include_partial: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> ReportMissingRequest:
        if self.to_date < self.from_date:
            raise ValueError("to must be on or after from")
        if (self.to_date - self.from_date).days > 730:
            raise ValueError("report missing range may not exceed 730 days")
        return self


class ReportContextArtifact(BaseModel):
    source: str
    remote_id: str
    kind: str
    title: str
    body_text: str | None
    state: str | None
    namespace: str | None
    url: str | None
    created_at_remote: datetime | None
    updated_at_remote: datetime | None
    versions: list[ArtifactVersionItem] = Field(default_factory=list)


class ReportContextExisting(BaseModel):
    kind: ReportKind
    status: ReportStatus
    current_revision: int
    source_snapshot_hash: str
    content_sha256: str
    updated_at: datetime


class ReportContextDailyDocument(BaseModel):
    period: date
    kind: ReportKind
    status: ReportStatus
    title: str
    markdown: str
    source_snapshot: dict[str, Any]


class ReportContextResponse(BaseModel):
    cadence: ReportCadence
    period: str
    period_start: date
    period_end: date
    from_time: datetime
    to_time: datetime
    activity_count: int
    source_event_counts: dict[str, int]
    activities: list[ActivityItem]
    artifacts: list[ReportContextArtifact]
    daily_documents: list[ReportContextDailyDocument] = Field(default_factory=list)
    source_snapshot: dict[str, Any]
    source_snapshot_hash: str
    all_sources_fresh: bool
    redaction_count: int = 0
    truncated: bool = False
    existing: list[ReportContextExisting] = Field(default_factory=list)


class ReportMissingItem(BaseModel):
    period: str
    status: Literal["missing", "partial"]
    missing_kinds: list[ReportKind] = Field(default_factory=list)
    partial_kinds: list[ReportKind] = Field(default_factory=list)


class ReportMissingResponse(BaseModel):
    cadence: ScheduledReportCadence
    items: list[ReportMissingItem]


class ReportUpsertRequest(BaseModel):
    status: ReportStatus
    title: str = Field(min_length=1, max_length=500)
    markdown: str = Field(min_length=1, max_length=1_048_576)
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    source_event_counts: dict[str, int] = Field(default_factory=dict)
    prompt_version: str = Field(min_length=1, max_length=64)
    generator_model: str = Field(min_length=1, max_length=128)

    @field_validator("markdown")
    @classmethod
    def validate_markdown(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("markdown may not contain NUL characters")
        return value


class ReportVersionSummary(BaseModel):
    revision: int
    status: ReportStatus
    content_sha256: str
    prompt_version: str
    generator_model: str
    created_at: datetime


class ReportItem(BaseModel):
    id: str
    cadence: ReportCadence
    period: str
    period_start: date
    period_end: date
    kind: ReportKind
    status: ReportStatus
    title: str
    markdown: str
    source_snapshot: dict[str, Any]
    source_event_counts: dict[str, int]
    prompt_version: str
    generator_model: str
    content_sha256: str
    current_revision: int
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None
    versions: list[ReportVersionSummary] = Field(default_factory=list)


class ReportListItem(BaseModel):
    id: str
    cadence: ReportCadence
    period: str
    kind: ReportKind
    status: ReportStatus
    title: str
    content_sha256: str
    current_revision: int
    updated_at: datetime


class ReportListResponse(BaseModel):
    items: list[ReportListItem]
