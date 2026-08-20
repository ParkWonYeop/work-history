#!/usr/bin/env python3
"""Remove explicitly identified raw-record fingerprints from a decrypted archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record["source"]),
        str(record["record_key"]),
        str(record["collected_at"]),
        str(record["payload_sha256"]),
    )


def _load_json_lines(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("archive input is missing or unsafe")
    with path.open("rb") as stream:
        first = stream.readline()
        if not first:
            raise RuntimeError("archive input is empty")
        manifest = json.loads(first)
        records = [json.loads(line) for line in stream if line.strip()]
    if manifest.get("type") != "manifest" or manifest.get("format_version") != 1:
        raise RuntimeError("unsupported archive manifest")
    if manifest.get("record_count") != len(records):
        raise RuntimeError("archive record count does not match manifest")
    counts: Counter[str] = Counter()
    for record in records:
        if record.get("type") != "raw_record":
            raise RuntimeError("unexpected archive record type")
        payload_hash = hashlib.sha256(canonical_json(record["payload"])).hexdigest()
        if payload_hash != record.get("payload_sha256"):
            raise RuntimeError("archive payload checksum mismatch")
        counts[str(record["source"])] += 1
    if dict(sorted(counts.items())) != manifest.get("source_counts"):
        raise RuntimeError("archive source counts do not match manifest")
    return manifest, records


def _load_exclusions(path: Path) -> set[tuple[str, str, str, str]]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("exclusion file is missing or unsafe")
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or not values:
        raise RuntimeError("exclusion file must contain a non-empty JSON array")
    exclusions = {fingerprint(value) for value in values}
    if len(exclusions) != len(values):
        raise RuntimeError("exclusion file contains duplicate fingerprints")
    return exclusions


def repack(
    input_path: Path,
    exclusions_path: Path,
    output_path: Path,
    batch_id: str,
    created_at: str,
) -> dict[str, Any]:
    _, records = _load_json_lines(input_path)
    exclusions = _load_exclusions(exclusions_path)
    matched = {fingerprint(record) for record in records if fingerprint(record) in exclusions}
    if matched != exclusions:
        missing = len(exclusions - matched)
        raise RuntimeError(f"archive does not contain {missing} requested exclusions")
    retained = [record for record in records if fingerprint(record) not in exclusions]
    source_counts = dict(sorted(Counter(item["source"] for item in retained).items()))
    manifest = {
        "batch_id": batch_id,
        "created_at": created_at,
        "format_version": 1,
        "record_count": len(retained),
        "source_counts": source_counts,
        "type": "manifest",
    }
    if output_path.exists() or output_path.is_symlink():
        raise RuntimeError("refusing to replace archive output")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=output_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_json(manifest) + b"\n")
            for record in retained:
                output.write(canonical_json(record) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "batch_id": batch_id,
        "excluded": len(exclusions),
        "records": len(retained),
        "source_counts": source_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("exclusions", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--created-at", required=True)
    args = parser.parse_args()
    datetime.fromisoformat(args.created_at.replace("Z", "+00:00"))
    result = repack(
        args.input,
        args.exclusions,
        args.output,
        args.batch_id,
        args.created_at,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
