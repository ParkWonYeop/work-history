#!/usr/bin/env python3
"""Perform checksum-pinned R2 archive replacement operations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from botocore.exceptions import ClientError

from work_history.config import Settings
from work_history.raw_archive import R2Store


def _validate_key(key: str, prefix: str) -> None:
    expected = f"{prefix.strip('/')}/"
    if not key.startswith(expected):
        raise RuntimeError("object key is outside the configured archive prefix")
    parts = key.removeprefix(expected).split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError("object key contains an unsafe path component")
    if not key.endswith(".jsonl.zst.age"):
        raise RuntimeError("object key does not name an encrypted archive")


def _is_missing(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _verify_local(path: Path, sha256: str, size: int) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("local archive is missing or unsafe")
    digest = hashlib.sha256()
    actual_size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            actual_size += len(chunk)
            digest.update(chunk)
    if actual_size != size or digest.hexdigest() != sha256:
        raise RuntimeError("local archive does not match the pinned size and checksum")


def upload_new(store: R2Store, path: Path, key: str, sha256: str, size: int) -> None:
    _verify_local(path, sha256, size)
    try:
        store.client.head_object(Bucket=store.bucket, Key=key)
    except ClientError as exc:
        if not _is_missing(exc):
            raise
    else:
        raise RuntimeError("refusing to replace an existing R2 object")
    store.upload_verified(path, key, sha256, size)


def delete_verified(store: R2Store, key: str, sha256: str, size: int) -> None:
    store.verify(key, sha256, size)
    store.client.delete_object(Bucket=store.bucket, Key=key)
    try:
        store.client.head_object(Bucket=store.bucket, Key=key)
    except ClientError as exc:
        if _is_missing(exc):
            return
        raise
    raise RuntimeError("R2 object still exists after deletion")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    upload = subparsers.add_parser("upload-new")
    upload.add_argument("--path", type=Path, required=True)
    delete = subparsers.add_parser("delete-verified")
    for subparser in (upload, delete):
        subparser.add_argument("--key", required=True)
        subparser.add_argument("--sha256", required=True)
        subparser.add_argument("--size", type=int, required=True)
    args = parser.parse_args()
    if len(args.sha256) != 64 or args.size < 1:
        raise RuntimeError("invalid pinned archive checksum or size")
    settings = Settings.from_env()
    settings.require_raw_archive()
    _validate_key(args.key, settings.raw_archive_r2_prefix)
    store = R2Store(settings)
    if args.command == "upload-new":
        upload_new(store, args.path, args.key, args.sha256, args.size)
    else:
        delete_verified(store, args.key, args.sha256, args.size)
    print(json.dumps({"command": args.command, "key": args.key, "status": "ok"}))


if __name__ == "__main__":
    main()
