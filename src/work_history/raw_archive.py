from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from work_history.config import Settings
from work_history.models import RawArchiveBatch, RawArchiveEntry, RawRecord, utcnow

FORMAT_VERSION = 1
VERIFIED_STATUS = "verified"
LOCAL_VERIFIED_STATUS = "local_verified"
SCAN_PAGE_SIZE = 100


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True)
class RawSnapshot:
    source: str
    record_key: str
    kind: str
    payload: dict[str, Any]
    collected_at: datetime
    expires_at: datetime
    payload_hash: str

    @classmethod
    def from_model(cls, record: RawRecord) -> RawSnapshot:
        return cls(
            source=record.source,
            record_key=record.record_key,
            kind=record.kind,
            payload=record.payload,
            collected_at=record.collected_at,
            expires_at=record.expires_at,
            payload_hash=payload_sha256(record.payload),
        )

    @property
    def fingerprint(self) -> tuple[str, str, str, str]:
        return (
            self.source,
            self.record_key,
            _utc_iso(self.collected_at),
            self.payload_hash,
        )

    def envelope(self) -> dict[str, Any]:
        return {
            "collected_at": _utc_iso(self.collected_at),
            "expires_at": _utc_iso(self.expires_at),
            "kind": self.kind,
            "payload": self.payload,
            "payload_sha256": self.payload_hash,
            "record_key": self.record_key,
            "source": self.source,
            "type": "raw_record",
        }


def verified_fingerprints(
    session: Session,
    snapshots: list[RawSnapshot],
) -> set[tuple[str, str, str, str]]:
    verified: set[tuple[str, str, str, str]] = set()
    for offset in range(0, len(snapshots), SCAN_PAGE_SIZE):
        page = snapshots[offset : offset + SCAN_PAGE_SIZE]
        clauses = [
            and_(
                RawArchiveEntry.source == item.source,
                RawArchiveEntry.record_key == item.record_key,
                RawArchiveEntry.collected_at == item.collected_at,
            )
            for item in page
        ]
        if not clauses:
            continue
        rows = session.execute(
            select(
                RawArchiveEntry.source,
                RawArchiveEntry.record_key,
                RawArchiveEntry.collected_at,
                RawArchiveEntry.payload_sha256,
            )
            .join(RawArchiveBatch)
            .where(RawArchiveBatch.status == VERIFIED_STATUS, or_(*clauses))
        )
        verified.update(
            (source, record_key, _utc_iso(collected_at), payload_hash)
            for source, record_key, collected_at, payload_hash in rows
        )
    return verified


@dataclass(frozen=True)
class EncryptedFile:
    plaintext_sha256: str
    ciphertext_sha256: str
    ciphertext_size: int


class R2Store:
    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.raw_archive_r2_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.raw_archive_r2_endpoint,
            aws_access_key_id=settings.raw_archive_r2_access_key_id,
            aws_secret_access_key=settings.raw_archive_r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
        )

    def check(self) -> dict[str, str]:
        self.client.head_bucket(Bucket=self.bucket)
        return {"bucket": self.bucket, "status": "ok"}

    def upload_verified(
        self,
        path: Path,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        self.client.upload_file(
            str(path),
            self.bucket,
            object_key,
            ExtraArgs={
                "ContentType": "application/octet-stream",
                "Metadata": {
                    "sha256": expected_sha256,
                    "format-version": str(FORMAT_VERSION),
                },
            },
        )
        self.verify(object_key, expected_sha256, expected_size)

    def ensure_uploaded(
        self,
        path: Path,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        """Reuse an already verified object instead of overwriting a locked key."""
        from botocore.exceptions import ClientError

        try:
            self.verify(object_key, expected_sha256, expected_size)
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code not in {"404", "NoSuchKey", "NotFound"} and status != 404:
                raise
        self.upload_verified(path, object_key, expected_sha256, expected_size)

    def verify(self, object_key: str, expected_sha256: str, expected_size: int) -> None:
        head = self.client.head_object(Bucket=self.bucket, Key=object_key)
        if int(head["ContentLength"]) != expected_size:
            raise RuntimeError("R2 archive size does not match the local archive")
        if (head.get("Metadata") or {}).get("sha256") != expected_sha256:
            raise RuntimeError("R2 archive metadata checksum does not match")

        response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        digest = hashlib.sha256()
        body = response["Body"]
        try:
            while chunk := body.read(1024 * 1024):
                digest.update(chunk)
        finally:
            body.close()
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError("R2 archive content checksum does not match")

    def fetch_verified(
        self,
        object_key: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        try:
            with _exclusive_binary_file(part) as stream:
                response = self.client.get_object(Bucket=self.bucket, Key=object_key)
                body = response["Body"]
                try:
                    shutil.copyfileobj(body, stream, length=1024 * 1024)
                finally:
                    body.close()
                stream.flush()
                os.fsync(stream.fileno())
            _verify_ciphertext(part, expected_sha256, expected_size)
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("refusing to replace an existing archive file")
            os.replace(part, destination)
            _fsync_directory(destination.parent)
        finally:
            part.unlink(missing_ok=True)


def _exclusive_binary_file(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_line(stream: BinaryIO, digest: Any, value: dict[str, Any]) -> None:
    data = _canonical_json(value) + b"\n"
    digest.update(data)
    stream.write(data)


def encrypt_snapshots(
    destination: Path,
    recipient: str,
    batch_id: str,
    created_at: datetime,
    snapshots: list[RawSnapshot],
) -> EncryptedFile:
    if not snapshots:
        raise RuntimeError("cannot create an empty raw archive")
    if not recipient.startswith("age1"):
        raise RuntimeError("invalid age recipient")
    for executable in ("zstd", "age"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"required archive executable is missing: {executable}")

    part = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("refusing to replace an existing archive file")
    ordered = sorted(
        snapshots,
        key=lambda item: (
            item.source,
            item.record_key,
            _utc_iso(item.collected_at),
            item.payload_hash,
        ),
    )
    plaintext_digest = hashlib.sha256()
    source_counts = dict(sorted(Counter(item.source for item in ordered).items()))
    manifest = {
        "batch_id": batch_id,
        "created_at": _utc_iso(created_at),
        "format_version": FORMAT_VERSION,
        "record_count": len(ordered),
        "source_counts": source_counts,
        "type": "manifest",
    }

    try:
        with _exclusive_binary_file(part) as output:
            compressor = subprocess.Popen(
                ["zstd", "-q", "-19", "-T0", "-c"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if compressor.stdin is None or compressor.stdout is None:
                raise RuntimeError("failed to open zstd pipeline")
            encryptor = subprocess.Popen(
                ["age", "-r", recipient],
                stdin=compressor.stdout,
                stdout=output,
                stderr=subprocess.PIPE,
            )
            compressor.stdout.close()
            try:
                _write_line(compressor.stdin, plaintext_digest, manifest)
                for snapshot in ordered:
                    _write_line(compressor.stdin, plaintext_digest, snapshot.envelope())
                compressor.stdin.close()
                encryptor_stderr = encryptor.communicate()[1]
                compressor_stderr = compressor.stderr.read() if compressor.stderr else b""
                compressor_status = compressor.wait()
            except Exception:
                compressor.kill()
                encryptor.kill()
                compressor.wait()
                encryptor.wait()
                raise
            if compressor_status != 0:
                raise RuntimeError(
                    f"zstd failed: {compressor_stderr.decode('utf-8', 'replace')[:1000]}"
                )
            if encryptor.returncode != 0:
                raise RuntimeError(
                    f"age failed: {encryptor_stderr.decode('utf-8', 'replace')[:1000]}"
                )
            output.flush()
            os.fsync(output.fileno())
        os.replace(part, destination)
        _fsync_directory(destination.parent)
    finally:
        part.unlink(missing_ok=True)

    ciphertext_digest = hashlib.sha256()
    size = 0
    with destination.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            ciphertext_digest.update(chunk)
    return EncryptedFile(
        plaintext_sha256=plaintext_digest.hexdigest(),
        ciphertext_sha256=ciphertext_digest.hexdigest(),
        ciphertext_size=size,
    )


def _verify_ciphertext(path: Path, expected_sha256: str, expected_size: int) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"archive file is missing or unsafe: {path}")
    stat = path.stat()
    if stat.st_size != expected_size:
        raise RuntimeError(f"local archive size mismatch: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError(f"local archive checksum mismatch: {path}")


class RawArchiveManager:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        store: R2Store,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.store = store
        self.base_dir = Path(settings.raw_archive_dir)

    def _validate_base_dir(self) -> None:
        if self.base_dir.is_symlink() or not self.base_dir.is_dir():
            raise RuntimeError("raw archive directory is missing or unsafe")
        resolved = self.base_dir.resolve(strict=True)
        if resolved != self.base_dir:
            raise RuntimeError("raw archive directory must use its canonical path")

    def _archive_path(self, object_key: str) -> Path:
        prefix = f"{self.settings.raw_archive_r2_prefix}/"
        if not object_key.startswith(prefix):
            raise RuntimeError("archive object is outside the configured prefix")
        parts = object_key.removeprefix(prefix).split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise RuntimeError("archive object contains an unsafe path component")
        parent = self.base_dir
        for part in parts[:-1]:
            parent = parent / part
            parent.mkdir(mode=0o700, exist_ok=True)
            if parent.is_symlink() or not parent.is_dir():
                raise RuntimeError("archive parent directory is unsafe")
        destination = parent / parts[-1]
        resolved_parent = destination.parent.resolve(strict=True)
        if self.base_dir not in (resolved_parent, *resolved_parent.parents):
            raise RuntimeError("archive path escaped the configured directory")
        if destination.is_symlink():
            raise RuntimeError("archive destination must not be a symbolic link")
        return destination

    def _next_snapshots(
        self,
        session: Session,
        cutoff: datetime,
        include_current: bool,
    ) -> list[RawSnapshot]:
        selected: list[RawSnapshot] = []
        offset = 0
        while len(selected) < self.settings.raw_archive_batch_size:
            query = select(RawRecord).order_by(RawRecord.source, RawRecord.record_key)
            if not include_current:
                query = query.where(RawRecord.expires_at <= cutoff)
            records = list(
                session.scalars(
                    query.offset(offset).limit(SCAN_PAGE_SIZE).with_for_update()
                )
            )
            if not records:
                break
            offset += len(records)
            snapshots = [RawSnapshot.from_model(record) for record in records]
            verified = verified_fingerprints(session, snapshots)
            for snapshot in snapshots:
                if snapshot.fingerprint not in verified:
                    selected.append(snapshot)
                    if len(selected) >= self.settings.raw_archive_batch_size:
                        break
        return selected

    def _new_batch(
        self,
        session: Session,
        snapshots: list[RawSnapshot],
        now: datetime,
    ) -> RawArchiveBatch:
        batch_id = str(uuid.uuid4())
        stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        month = now.astimezone(UTC).strftime("%Y/%m")
        object_key = (
            f"{self.settings.raw_archive_r2_prefix}/{month}/"
            f"{stamp}-{batch_id}.jsonl.zst.age"
        )
        local_path = self._archive_path(object_key)
        source_counts = dict(sorted(Counter(item.source for item in snapshots).items()))
        batch = RawArchiveBatch(
            id=batch_id,
            format_version=FORMAT_VERSION,
            object_key=object_key,
            local_path=str(local_path),
            status="creating",
            record_count=len(snapshots),
            source_counts=source_counts,
            earliest_collected_at=min(item.collected_at for item in snapshots),
            latest_collected_at=max(item.collected_at for item in snapshots),
            created_at=now,
        )
        session.add(batch)
        for item in snapshots:
            session.add(
                RawArchiveEntry(
                    batch_id=batch_id,
                    source=item.source,
                    record_key=item.record_key,
                    kind=item.kind,
                    collected_at=item.collected_at,
                    expires_at=item.expires_at,
                    payload_sha256=item.payload_hash,
                )
            )
        return batch

    def _mark_error(self, batch_id: str, status: str, error: Exception) -> None:
        with self.session_factory() as session:
            batch = session.get(RawArchiveBatch, batch_id)
            if batch:
                batch.status = status
                batch.error = str(error)[:4000]
                session.commit()

    def _upload_and_finish(self, batch_id: str) -> RawArchiveBatch:
        with self.session_factory() as session:
            batch = session.get(RawArchiveBatch, batch_id)
            if batch is None:
                raise RuntimeError("archive batch disappeared")
            if not batch.ciphertext_sha256 or batch.ciphertext_size is None:
                raise RuntimeError("archive batch has no local checksum")
            path = Path(batch.local_path)
            _verify_ciphertext(path, batch.ciphertext_sha256, batch.ciphertext_size)
            object_key = batch.object_key
            checksum = batch.ciphertext_sha256
            size = batch.ciphertext_size
        self.store.ensure_uploaded(path, object_key, checksum, size)
        with self.session_factory() as session:
            batch = session.get(RawArchiveBatch, batch_id)
            if batch is None:
                raise RuntimeError("archive batch disappeared after upload")
            batch.status = VERIFIED_STATUS
            batch.remote_verified_at = utcnow()
            batch.error = None
            session.commit()
            return batch

    def recover_pending(self) -> int:
        with self.session_factory() as session:
            pending = list(
                session.scalars(
                    select(RawArchiveBatch).where(
                        RawArchiveBatch.status == LOCAL_VERIFIED_STATUS
                    )
                )
            )
        recovered = 0
        for batch in pending:
            try:
                self._upload_and_finish(batch.id)
                recovered += 1
            except Exception as exc:
                self._mark_error(batch.id, LOCAL_VERIFIED_STATUS, exc)
                raise
        return recovered

    def create_one(self, cutoff: datetime, include_current: bool = False) -> str | None:
        self._validate_base_dir()
        now = utcnow()
        with self.session_factory() as session:
            snapshots = self._next_snapshots(session, cutoff, include_current)
            if not snapshots:
                session.rollback()
                return None
            batch = self._new_batch(session, snapshots, now)
            batch_id = batch.id
            local_path = Path(batch.local_path)
            session.commit()

        try:
            encrypted = encrypt_snapshots(
                local_path,
                self.settings.raw_archive_age_recipient,
                batch_id,
                now,
                snapshots,
            )
        except Exception as exc:
            self._mark_error(batch_id, "failed", exc)
            raise

        with self.session_factory() as session:
            batch = session.get(RawArchiveBatch, batch_id)
            if batch is None:
                raise RuntimeError("archive batch disappeared after encryption")
            batch.plaintext_sha256 = encrypted.plaintext_sha256
            batch.ciphertext_sha256 = encrypted.ciphertext_sha256
            batch.ciphertext_size = encrypted.ciphertext_size
            batch.local_verified_at = utcnow()
            batch.status = LOCAL_VERIFIED_STATUS
            session.commit()
        try:
            self._upload_and_finish(batch_id)
        except Exception as exc:
            self._mark_error(batch_id, LOCAL_VERIFIED_STATUS, exc)
            raise
        return batch_id

    def archive_pending(
        self,
        *,
        include_current: bool = False,
        max_batches: int | None = None,
    ) -> dict[str, int]:
        self.recover_pending()
        cutoff = utcnow() + timedelta(days=self.settings.raw_archive_lookahead_days)
        batches = 0
        records = 0
        while max_batches is None or batches < max_batches:
            batch_id = self.create_one(cutoff, include_current=include_current)
            if batch_id is None:
                break
            with self.session_factory() as session:
                batch = session.get(RawArchiveBatch, batch_id)
                records += batch.record_count if batch else 0
            batches += 1
        return {"batches": batches, "records": records}

    def verify_all(self, *, remote: bool = True) -> dict[str, int]:
        self._validate_base_dir()
        with self.session_factory() as session:
            batches = list(
                session.scalars(
                    select(RawArchiveBatch)
                    .where(RawArchiveBatch.status == VERIFIED_STATUS)
                    .order_by(RawArchiveBatch.created_at)
                )
            )
        checked = 0
        for batch in batches:
            if not batch.ciphertext_sha256 or batch.ciphertext_size is None:
                raise RuntimeError(f"verified archive lacks checksum: {batch.id}")
            _verify_ciphertext(
                Path(batch.local_path), batch.ciphertext_sha256, batch.ciphertext_size
            )
            if remote:
                self.store.verify(
                    batch.object_key, batch.ciphertext_sha256, batch.ciphertext_size
                )
            checked += 1
        return {"batches": checked}

    def fetch(self, batch_id: str, destination: Path) -> dict[str, Any]:
        with self.session_factory() as session:
            batch = session.get(RawArchiveBatch, batch_id)
            if batch is None or batch.status != VERIFIED_STATUS:
                raise RuntimeError("verified archive batch not found")
            if not batch.ciphertext_sha256 or batch.ciphertext_size is None:
                raise RuntimeError("archive batch lacks checksum")
            self.store.fetch_verified(
                batch.object_key,
                destination,
                batch.ciphertext_sha256,
                batch.ciphertext_size,
            )
            return {
                "batch_id": batch.id,
                "path": str(destination),
                "sha256": batch.ciphertext_sha256,
                "size": batch.ciphertext_size,
            }


def list_archive_batches(session: Session) -> list[dict[str, Any]]:
    batches = session.scalars(
        select(RawArchiveBatch).order_by(RawArchiveBatch.created_at.desc())
    )
    return [
        {
            "batch_id": batch.id,
            "created_at": _utc_iso(batch.created_at),
            "object_key": batch.object_key,
            "record_count": batch.record_count,
            "source_counts": batch.source_counts,
            "sha256": batch.ciphertext_sha256,
            "size": batch.ciphertext_size,
            "status": batch.status,
            "local_verified_at": (
                _utc_iso(batch.local_verified_at) if batch.local_verified_at else None
            ),
            "remote_verified_at": (
                _utc_iso(batch.remote_verified_at) if batch.remote_verified_at else None
            ),
            "error": batch.error,
        }
        for batch in batches
    ]
