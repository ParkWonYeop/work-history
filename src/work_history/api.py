from __future__ import annotations

import gzip
import io
import json
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from work_history.config import Settings, get_settings
from work_history.db import create_database_engine, create_session_factory
from work_history.models import (
    ActivityEvent,
    Artifact,
    Base,
    GeneratedReport,
    IngestDevice,
    SyncCursor,
    SyncRun,
)
from work_history.reports import (
    build_report_context,
    missing_report_periods,
    parse_period,
    period_bounds,
    upsert_generated_report,
)
from work_history.schemas import (
    ActivityItem,
    ActivityPage,
    ArtifactItem,
    ArtifactVersionItem,
    CheckpointResponse,
    GitLabIngestBatch,
    IngestResponse,
    ReportContextRequest,
    ReportContextResponse,
    ReportItem,
    ReportListItem,
    ReportListResponse,
    ReportMissingRequest,
    ReportMissingResponse,
    ReportUpsertRequest,
    ReportVersionSummary,
)
from work_history.security import (
    bearer_matches,
    sha256_hex,
    timestamp_is_fresh,
    verify_request_signature,
)
from work_history.services import (
    get_cursor,
    ingest_gitlab_batch,
    remember_nonce,
)


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._requests[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        raw = urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        occurred_at = datetime.fromisoformat(payload["occurred_at"])
        if occurred_at.tzinfo is None:
            raise ValueError
        return occurred_at.astimezone(UTC), str(payload["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


def _encode_cursor(event: ActivityEvent) -> str:
    occurred_at = event.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    value = json.dumps(
        {"occurred_at": occurred_at.isoformat(), "id": event.id},
        separators=(",", ":"),
    ).encode()
    return urlsafe_b64encode(value).rstrip(b"=").decode()


def _read_gzip_limited(body: bytes, maximum: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as stream:
            result = stream.read(maximum + 1)
    except (OSError, EOFError) as exc:
        raise HTTPException(status_code=400, detail="invalid gzip body") from exc
    if len(result) > maximum:
        raise HTTPException(status_code=413, detail="uncompressed body is too large")
    return result


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    create_schema: bool = False,
) -> FastAPI:
    settings = settings or get_settings()
    if session_factory is None:
        engine = create_database_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        if create_schema:
            Base.metadata.create_all(engine)

    app = FastAPI(
        title="Work History API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.read_limiter = SlidingWindowLimiter(limit=30)
    app.state.ingest_limiter = SlidingWindowLimiter(limit=60)

    def db_session() -> Any:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def client_key(request: Request, category: str) -> str:
        host = request.client.host if request.client else "unknown"
        return f"{category}:{host}"

    def require_read_token(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if not app.state.read_limiter.allow(client_key(request, "read")):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if not bearer_matches(token, settings.read_api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
            )

    async def authenticate_device(
        request: Request,
        session: Session,
        body: bytes,
        required_purpose: str,
    ) -> str:
        if not app.state.ingest_limiter.allow(client_key(request, required_purpose)):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        device_id = request.headers.get("X-WorkHistory-Device")
        sent_at = request.headers.get("X-WorkHistory-Timestamp")
        nonce = request.headers.get("X-WorkHistory-Nonce")
        signature = request.headers.get("X-WorkHistory-Signature")
        if not all((device_id, sent_at, nonce, signature)):
            raise HTTPException(status_code=401, detail="missing device signature headers")
        if not timestamp_is_fresh(sent_at, settings.signature_max_age_seconds):
            raise HTTPException(status_code=401, detail="expired request timestamp")
        device = session.get(IngestDevice, device_id)
        if not device or not device.enabled or device.revoked_at:
            raise HTTPException(status_code=401, detail="unknown or revoked device")
        if device.purpose != required_purpose:
            raise HTTPException(
                status_code=403,
                detail="device is not authorized for this endpoint",
            )
        if not verify_request_signature(
            device.public_key_b64,
            signature,
            request.method,
            request.url.path,
            sent_at,
            nonce,
            body,
        ):
            raise HTTPException(status_code=401, detail="invalid request signature")
        if not remember_nonce(
            session,
            device_id,
            nonce,
            settings.signature_max_age_seconds,
        ):
            raise HTTPException(status_code=409, detail="replayed request nonce")
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="replayed request nonce") from exc
        return device_id

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz(session: Session = Depends(db_session)) -> dict[str, str]:
        try:
            session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    @app.get(
        "/v1/sync-status",
        dependencies=[Depends(require_read_token)],
    )
    def sync_status(session: Session = Depends(db_session)) -> dict[str, Any]:
        runs = session.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(100)).all()
        latest: dict[str, Any] = {}
        for run in runs:
            if run.source in latest:
                continue
            latest[run.source] = {
                "status": run.status,
                "job_kind": run.job_kind,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "counters": run.counters,
                "error": run.error,
            }
        gitlab_cursors = session.scalars(
            select(SyncCursor)
            .where(SyncCursor.source == "gitlab")
            .order_by(SyncCursor.updated_at.desc())
        ).all()
        if gitlab_cursors:
            newest = gitlab_cursors[0]
            latest["gitlab"] = {
                "status": "success",
                "job_kind": "signed_ingest",
                "started_at": None,
                "finished_at": newest.updated_at,
                "counters": {"devices": len(gitlab_cursors)},
                "error": None,
            }
        return {"sources": latest}

    @app.get(
        "/v1/activities",
        response_model=ActivityPage,
        dependencies=[Depends(require_read_token)],
    )
    def activities(
        from_: datetime = Query(alias="from"),
        to: datetime = Query(),
        sources: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
        session: Session = Depends(db_session),
    ) -> ActivityPage:
        if from_.tzinfo is None or to.tzinfo is None:
            raise HTTPException(status_code=400, detail="from and to require timezone offsets")
        from_ = from_.astimezone(UTC)
        to = to.astimezone(UTC)
        if to <= from_:
            raise HTTPException(status_code=400, detail="to must be after from")
        if to - from_ > timedelta(days=31):
            raise HTTPException(status_code=400, detail="maximum range is 31 days")

        conditions: list[Any] = [
            ActivityEvent.occurred_at >= from_,
            ActivityEvent.occurred_at < to,
        ]
        if sources:
            source_values = [part.strip() for part in sources.split(",") if part.strip()]
            invalid = set(source_values) - {"jira", "confluence", "gitlab"}
            if invalid:
                raise HTTPException(status_code=400, detail="invalid source filter")
            conditions.append(ActivityEvent.source.in_(source_values))
        if cursor:
            cursor_time, cursor_id = _decode_cursor(cursor)
            conditions.append(
                or_(
                    ActivityEvent.occurred_at > cursor_time,
                    and_(
                        ActivityEvent.occurred_at == cursor_time,
                        ActivityEvent.id > cursor_id,
                    ),
                )
            )
        rows = session.scalars(
            select(ActivityEvent)
            .where(*conditions)
            .order_by(ActivityEvent.occurred_at.asc(), ActivityEvent.id.asc())
            .limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return ActivityPage(
            items=[ActivityItem.model_validate(row) for row in rows],
            next_cursor=_encode_cursor(rows[-1]) if has_more and rows else None,
        )

    @app.get(
        "/v1/artifacts/{source}/{remote_id:path}",
        response_model=ArtifactItem,
        dependencies=[Depends(require_read_token)],
    )
    def artifact(
        source: str,
        remote_id: str,
        session: Session = Depends(db_session),
    ) -> ArtifactItem:
        if source not in {"jira", "confluence", "gitlab"}:
            raise HTTPException(status_code=404, detail="artifact not found")
        model = session.scalar(
            select(Artifact)
            .options(selectinload(Artifact.versions))
            .where(Artifact.source == source, Artifact.remote_id == remote_id)
        )
        if model is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return ArtifactItem(
            id=model.id,
            source=model.source,
            remote_id=model.remote_id,
            kind=model.kind,
            title=model.title,
            body_text=model.body_text,
            state=model.state,
            namespace=model.namespace,
            url=model.url,
            created_at_remote=model.created_at_remote,
            updated_at_remote=model.updated_at_remote,
            versions=[ArtifactVersionItem.model_validate(item) for item in model.versions],
        )

    @app.get(
        "/v1/ingest/gitlab/checkpoint",
        response_model=CheckpointResponse,
    )
    async def gitlab_checkpoint(
        request: Request,
        session: Session = Depends(db_session),
    ) -> CheckpointResponse:
        device_id = await authenticate_device(request, session, b"", "gitlab_ingest")
        return CheckpointResponse(
            device_id=device_id,
            checkpoint=get_cursor(session, "gitlab", device_id),
        )

    @app.post(
        "/v1/ingest/gitlab/batches",
        response_model=IngestResponse,
    )
    async def gitlab_batches(
        request: Request,
        session: Session = Depends(db_session),
    ) -> IngestResponse:
        wire_body = await request.body()
        if len(wire_body) > settings.ingest_max_uncompressed_bytes + 1024 * 1024:
            raise HTTPException(status_code=413, detail="request body is too large")
        device_id = await authenticate_device(request, session, wire_body, "gitlab_ingest")
        if request.headers.get("Content-Encoding", "").lower() == "gzip":
            body = _read_gzip_limited(
                wire_body,
                settings.ingest_max_uncompressed_bytes,
            )
        else:
            body = wire_body
            if len(body) > settings.ingest_max_uncompressed_bytes:
                raise HTTPException(status_code=413, detail="request body is too large")
        try:
            payload = GitLabIngestBatch.model_validate_json(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            result = ingest_gitlab_batch(
                session,
                payload,
                device_id,
                sha256_hex(body),
                settings.raw_retention_days,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        return IngestResponse(**result)

    @app.post(
        "/v1/report-agent/context",
        response_model=ReportContextResponse,
    )
    async def report_context(
        request: Request,
        session: Session = Depends(db_session),
    ) -> ReportContextResponse:
        body = await request.body()
        if len(body) > 16_384:
            raise HTTPException(status_code=413, detail="request body is too large")
        await authenticate_device(request, session, body, "report_agent")
        try:
            payload = ReportContextRequest.model_validate_json(body)
            parsed_period = parse_period(payload.cadence, payload.period)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ReportContextResponse.model_validate(
            build_report_context(session, payload.cadence, parsed_period)
        )

    @app.post(
        "/v1/report-agent/missing",
        response_model=ReportMissingResponse,
    )
    async def report_missing(
        request: Request,
        session: Session = Depends(db_session),
    ) -> ReportMissingResponse:
        body = await request.body()
        if len(body) > 16_384:
            raise HTTPException(status_code=413, detail="request body is too large")
        await authenticate_device(request, session, body, "report_agent")
        try:
            payload = ReportMissingRequest.model_validate_json(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        items = missing_report_periods(
            session,
            payload.cadence,
            payload.from_date,
            payload.to_date,
            payload.include_partial,
        )
        return ReportMissingResponse(cadence=payload.cadence, items=items)

    @app.put(
        "/v1/reports/{cadence}/{period}/{kind}",
        response_model=ReportItem,
    )
    async def put_report(
        cadence: Literal["daily", "monthly"],
        period: str,
        kind: Literal["work_report", "feedback"],
        request: Request,
        session: Session = Depends(db_session),
    ) -> ReportItem:
        body = await request.body()
        if len(body) > 1_100_000:
            raise HTTPException(status_code=413, detail="request body is too large")
        await authenticate_device(request, session, body, "report_agent")
        try:
            parsed_period = parse_period(cadence, period)
            payload = ReportUpsertRequest.model_validate_json(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        report, _ = upsert_generated_report(
            session,
            cadence,
            parsed_period,
            kind,
            payload.model_dump(mode="json"),
        )
        session.commit()
        session.refresh(report)
        return _report_item(report)

    @app.get(
        "/v1/reports",
        response_model=ReportListResponse,
        dependencies=[Depends(require_read_token)],
    )
    def list_reports(
        cadence: Literal["daily", "monthly"] | None = Query(default=None),
        kind: Literal["work_report", "feedback"] | None = Query(default=None),
        status_: Literal["partial", "final"] | None = Query(default=None, alias="status"),
        from_: date | None = Query(default=None, alias="from"),
        to: date | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
        session: Session = Depends(db_session),
    ) -> ReportListResponse:
        conditions: list[Any] = []
        if cadence:
            conditions.append(GeneratedReport.cadence == cadence)
        if kind:
            conditions.append(GeneratedReport.kind == kind)
        if status_:
            conditions.append(GeneratedReport.status == status_)
        if from_:
            conditions.append(GeneratedReport.period_start >= from_)
        if to:
            conditions.append(GeneratedReport.period_start <= to)
        rows = session.scalars(
            select(GeneratedReport)
            .where(*conditions)
            .order_by(GeneratedReport.period_start.desc(), GeneratedReport.kind.asc())
            .limit(limit)
        ).all()
        return ReportListResponse(
            items=[
                ReportListItem(
                    id=row.id,
                    cadence=row.cadence,
                    period=(
                        row.period_start.isoformat()
                        if row.cadence == "daily"
                        else row.period_start.strftime("%Y-%m")
                    ),
                    kind=row.kind,
                    status=row.status,
                    title=row.title,
                    content_sha256=row.content_sha256,
                    current_revision=row.current_revision,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]
        )

    @app.get(
        "/v1/reports/{cadence}/{period}/{kind}",
        response_model=ReportItem,
        dependencies=[Depends(require_read_token)],
    )
    def get_report(
        cadence: Literal["daily", "monthly"],
        period: str,
        kind: Literal["work_report", "feedback"],
        session: Session = Depends(db_session),
    ) -> ReportItem:
        try:
            parsed_period = parse_period(cadence, period)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        period_start, _, _ = period_bounds(cadence, parsed_period)
        report = session.scalar(
            select(GeneratedReport)
            .options(selectinload(GeneratedReport.versions))
            .where(
                GeneratedReport.cadence == cadence,
                GeneratedReport.period_start == period_start,
                GeneratedReport.kind == kind,
            )
        )
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        return _report_item(report)

    return app


def _report_item(report: GeneratedReport) -> ReportItem:
    return ReportItem(
        id=report.id,
        cadence=report.cadence,
        period=(
            report.period_start.isoformat()
            if report.cadence == "daily"
            else report.period_start.strftime("%Y-%m")
        ),
        period_start=report.period_start,
        period_end=report.period_end,
        kind=report.kind,
        status=report.status,
        title=report.title,
        markdown=report.markdown,
        source_snapshot=report.source_snapshot,
        source_event_counts=report.source_event_counts,
        prompt_version=report.prompt_version,
        generator_model=report.generator_model,
        content_sha256=report.content_sha256,
        current_revision=report.current_revision,
        created_at=report.created_at,
        updated_at=report.updated_at,
        finalized_at=report.finalized_at,
        versions=[
            ReportVersionSummary(
                revision=item.revision,
                status=item.status,
                content_sha256=item.content_sha256,
                prompt_version=item.prompt_version,
                generator_model=item.generator_model,
                created_at=item.created_at,
            )
            for item in report.versions
        ],
    )


app = create_app()
