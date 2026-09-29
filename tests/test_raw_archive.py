from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from work_history.models import RawArchiveBatch, RawRecord
from work_history.raw_archive import (
    EncryptedFile,
    RawArchiveManager,
    RawSnapshot,
    encrypt_snapshots,
    payload_sha256,
    upload_db_backup,
)
from work_history.services import cleanup_expired


class FakeStore:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.objects: dict[str, bytes] = {}

    def upload_verified(
        self,
        path: Path,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        if self.fail_upload:
            raise RuntimeError("R2 unavailable")
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        assert len(data) == expected_size
        self.objects[object_key] = data

    def ensure_uploaded(
        self,
        path: Path,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        if object_key in self.objects:
            self.verify(object_key, expected_sha256, expected_size)
            return
        self.upload_verified(path, object_key, expected_sha256, expected_size)

    def verify(self, object_key: str, expected_sha256: str, expected_size: int) -> None:
        data = self.objects[object_key]
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        assert len(data) == expected_size

    def list_objects(self, prefix: str = "") -> dict[str, int]:
        return {key: len(data) for key, data in self.objects.items() if key.startswith(prefix)}

    def delete_object(self, object_key: str) -> None:
        del self.objects[object_key]

    def fetch_verified(
        self,
        object_key: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        data = self.objects[object_key]
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        assert len(data) == expected_size
        destination.write_bytes(data)


def _fake_encrypt(
    destination: Path,
    recipient: str,
    batch_id: str,
    created_at: datetime,
    snapshots: list[RawSnapshot],
) -> EncryptedFile:
    del recipient, created_at
    payload = b"\n".join(
        json.dumps(
            {"batch_id": batch_id, **snapshot.envelope()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        for snapshot in snapshots
    )
    destination.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return EncryptedFile(
        plaintext_sha256=digest,
        ciphertext_sha256=digest,
        ciphertext_size=len(payload),
    )


def _archive_settings(settings, tmp_path: Path, *, batch_size: int = 5000):
    return replace(
        settings,
        raw_archive_dir=str(tmp_path),
        raw_archive_age_recipient="age1testrecipient",
        raw_archive_r2_endpoint="https://r2.example.com",
        raw_archive_r2_bucket="work-history-archive",
        raw_archive_r2_prefix="raw/v1",
        raw_archive_r2_access_key_id="access",
        raw_archive_r2_secret_access_key="secret",
        raw_archive_batch_size=batch_size,
    )


def test_payload_hash_is_canonical() -> None:
    assert payload_sha256({"b": 2, "a": {"z": 1}}) == payload_sha256(
        {"a": {"z": 1}, "b": 2}
    )


@pytest.mark.skipif(
    shutil.which("age-keygen") is None or shutil.which("zstd") is None,
    reason="age and zstd are required for the real archive round trip",
)
def test_real_encryption_round_trip_is_sorted_and_hashes_payload(tmp_path) -> None:
    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    now = datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC)
    snapshots = [
        RawSnapshot(
            source=source,
            record_key=key,
            kind="record",
            payload=payload,
            collected_at=now,
            expires_at=now + timedelta(days=180),
            payload_hash=payload_sha256(payload),
        )
        for source, key, payload in (
            ("slack", "z", {"text": "last"}),
            ("jira", "a", {"summary": "first"}),
        )
    ]
    archive = tmp_path / "archive.jsonl.zst.age"
    encrypted = encrypt_snapshots(
        archive, recipient, "roundtrip", now, snapshots
    )

    decryptor = subprocess.Popen(
        ["age", "-d", "-i", str(identity), str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert decryptor.stdout is not None
    decompressed = subprocess.run(
        ["zstd", "-q", "-d", "-c"],
        stdin=decryptor.stdout,
        check=True,
        capture_output=True,
    ).stdout
    decryptor.stdout.close()
    _, decrypt_error = decryptor.communicate()
    assert decryptor.returncode == 0, decrypt_error.decode()

    assert hashlib.sha256(decompressed).hexdigest() == encrypted.plaintext_sha256
    lines = [json.loads(line) for line in decompressed.splitlines()]
    assert lines[0]["record_count"] == 2
    assert [(item["source"], item["record_key"]) for item in lines[1:]] == [
        ("jira", "a"),
        ("slack", "z"),
    ]
    assert all(
        item["payload_sha256"] == payload_sha256(item["payload"])
        for item in lines[1:]
    )


def test_verified_archive_gates_cleanup_and_changed_records_are_rearchived(
    settings,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("work_history.raw_archive.encrypt_snapshots", _fake_encrypt)
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add_all(
            [
                RawRecord(
                    source="jira",
                    record_key="old",
                    kind="issue",
                    payload={"value": "old"},
                    collected_at=now - timedelta(days=181),
                    expires_at=now - timedelta(days=1),
                ),
                RawRecord(
                    source="slack",
                    record_key="current",
                    kind="message",
                    payload={"value": "current"},
                    collected_at=now,
                    expires_at=now + timedelta(days=180),
                ),
            ]
        )
        session.commit()

    manager = RawArchiveManager(
        _archive_settings(settings, tmp_path, batch_size=1),
        session_factory,
        FakeStore(),
    )
    assert manager.archive_pending(include_current=True) == {"batches": 2, "records": 2}

    with session_factory() as session:
        result = cleanup_expired(session)
        session.commit()
        assert result["raw_records"] == 1
        current = session.scalar(select(RawRecord).where(RawRecord.record_key == "current"))
        assert current is not None
        current.payload = {"value": "changed after archive"}
        current.collected_at = now + timedelta(minutes=1)
        current.expires_at = now - timedelta(seconds=1)
        session.commit()

    with session_factory() as session:
        result = cleanup_expired(session)
        session.commit()
        assert result == {
            "raw_records": 0,
            "unarchived_raw_records": 1,
            "nonces": 0,
        }

    assert manager.archive_pending() == {"batches": 1, "records": 1}
    with session_factory() as session:
        result = cleanup_expired(session)
        session.commit()
        assert result["raw_records"] == 1
        assert session.scalar(select(func.count()).select_from(RawRecord)) == 0


def test_remote_failure_retains_raw_record(
    settings,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("work_history.raw_archive.encrypt_snapshots", _fake_encrypt)
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            RawRecord(
                source="gitlab",
                record_key="commit:1",
                kind="commit",
                payload={"message": "keep me"},
                collected_at=now - timedelta(days=181),
                expires_at=now - timedelta(days=1),
            )
        )
        session.commit()

    manager = RawArchiveManager(
        _archive_settings(settings, tmp_path),
        session_factory,
        FakeStore(fail_upload=True),
    )
    with pytest.raises(RuntimeError, match="R2 unavailable"):
        manager.archive_pending()

    with session_factory() as session:
        batch = session.scalar(select(RawArchiveBatch))
        assert batch is not None
        assert batch.status == "local_verified"
        result = cleanup_expired(session)
        session.commit()
        assert result["raw_records"] == 0
        assert result["unarchived_raw_records"] == 1
        assert session.scalar(select(func.count()).select_from(RawRecord)) == 1


def test_retry_reuses_existing_remote_object_without_overwrite(
    settings,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("work_history.raw_archive.encrypt_snapshots", _fake_encrypt)
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            RawRecord(
                source="jira",
                record_key="retry",
                kind="issue",
                payload={"summary": "already uploaded"},
                collected_at=now,
                expires_at=now + timedelta(days=180),
            )
        )
        session.commit()

    store = FakeStore()
    manager = RawArchiveManager(
        _archive_settings(settings, tmp_path), session_factory, store
    )
    manager.archive_pending(include_current=True)
    with session_factory() as session:
        batch = session.scalar(select(RawArchiveBatch))
        assert batch is not None
        object_key = batch.object_key
        original = store.objects[object_key]
        batch.status = "local_verified"
        batch.remote_verified_at = None
        session.commit()

    store.fail_upload = True
    assert manager.recover_pending() == 1
    assert store.objects[object_key] == original


def test_local_corruption_is_detected_before_remote_verification(
    settings,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("work_history.raw_archive.encrypt_snapshots", _fake_encrypt)
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            RawRecord(
                source="confluence",
                record_key="page:1",
                kind="page",
                payload={"body": "text"},
                collected_at=now,
                expires_at=now + timedelta(days=180),
            )
        )
        session.commit()
    store = FakeStore()
    manager = RawArchiveManager(
        _archive_settings(settings, tmp_path), session_factory, store
    )
    manager.archive_pending(include_current=True)

    with session_factory() as session:
        batch = session.scalar(select(RawArchiveBatch))
        assert batch is not None
        Path(batch.local_path).write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="local archive size mismatch"):
        manager.verify_all()


def _expired_record(key: str, now: datetime) -> RawRecord:
    return RawRecord(
        source="jira",
        record_key=key,
        kind="issue",
        payload={"value": key},
        collected_at=now - timedelta(days=181),
        expires_at=now - timedelta(days=1),
    )


def test_cleanup_deletes_in_batches_and_keeps_unarchived_records(
    settings, session_factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("work_history.raw_archive.encrypt_snapshots", _fake_encrypt)
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add_all([_expired_record(f"archived-{index}", now) for index in range(3)])
        session.commit()
    manager = RawArchiveManager(_archive_settings(settings, tmp_path), session_factory, FakeStore())
    assert manager.archive_pending() == {"batches": 1, "records": 3}
    with session_factory() as session:
        session.add(_expired_record("unarchived", now))
        session.commit()

    with session_factory() as session:
        result = cleanup_expired(session, batch_size=2)
    assert result == {"raw_records": 3, "unarchived_raw_records": 1, "nonces": 0}
    with session_factory() as session:
        assert session.scalars(select(RawRecord.record_key)).all() == ["unarchived"]


def test_inventory_compares_the_bucket_listing_with_the_ledger(
    settings, session_factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("work_history.raw_archive.encrypt_snapshots", _fake_encrypt)
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add_all([_expired_record(f"record-{index}", now) for index in range(2)])
        session.commit()
    store = FakeStore()
    manager = RawArchiveManager(
        _archive_settings(settings, tmp_path, batch_size=1), session_factory, store
    )
    manager.archive_pending()
    verified_keys = sorted(store.objects)
    total = sum(len(data) for data in store.objects.values())
    assert manager.inventory() == {
        "objects": 2,
        "bytes": total,
        "raw_objects": 2,
        "raw_bytes": total,
        "db_verified_batches": 2,
        "db_verified_bytes": total,
        "missing_verified_objects": 0,
        "size_mismatches": 0,
        "untracked_raw_objects": 0,
    }

    store.objects["raw/v1/stray.jsonl.zst.age"] = b"x"
    store.objects["db/v1/2026/09/workhistory-20260901T023000Z.dump.age"] = b"yy"
    del store.objects[verified_keys[0]]
    store.objects[verified_keys[1]] += b"!"
    inventory = manager.inventory()
    assert inventory["objects"] == 3
    assert inventory["raw_objects"] == 2
    assert inventory["missing_verified_objects"] == 1
    assert inventory["size_mismatches"] == 1
    assert inventory["untracked_raw_objects"] == 1


@pytest.mark.skipif(
    shutil.which("age-keygen") is None or shutil.which("age") is None,
    reason="age is required for the database backup round trip",
)
def test_db_backup_is_encrypted_uploaded_and_old_copies_are_pruned(settings, tmp_path) -> None:
    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True
    ).stdout.strip()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    dump = backup_dir / "workhistory-20260929T023000Z.dump"
    dump.write_bytes(b"PGDMP custom dump bytes")
    store = FakeStore()
    old_keys = [
        "db/v1/2026/07/workhistory-20260701T023000Z.dump.age",
        "db/v1/2026/08/workhistory-20260801T023000Z.dump.age",
        "db/v1/2026/09/workhistory-20260901T023000Z.dump.age",
    ]
    for key in old_keys:
        store.objects[key] = b"old"
    store.objects["db/v1/README.txt"] = b"not a backup"

    result = upload_db_backup(
        replace(_archive_settings(settings, tmp_path), raw_archive_age_recipient=recipient),
        store,
        dump,
        keep=2,
    )

    assert result["pruned"] == 2
    assert result["prune_errors"] == 0
    assert set(store.objects) == {old_keys[2], result["object_key"], "db/v1/README.txt"}
    assert sorted(path.name for path in backup_dir.iterdir()) == [dump.name]
    encrypted = tmp_path / "downloaded.age"
    encrypted.write_bytes(store.objects[result["object_key"]])
    decrypted = subprocess.run(
        ["age", "-d", "-i", str(identity), str(encrypted)], check=True, capture_output=True
    ).stdout
    assert decrypted == dump.read_bytes()


def test_db_backup_refuses_unexpected_file_names(settings, tmp_path) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("secret")
    with pytest.raises(RuntimeError, match="unexpected database dump name"):
        upload_db_backup(_archive_settings(settings, tmp_path), FakeStore(), other)
