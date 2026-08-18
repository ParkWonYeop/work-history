#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: verify-archive.py ARCHIVE.jsonl")
    path = Path(sys.argv[1])
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("archive JSONL is missing or unsafe")

    count = 0
    sources: Counter[str] = Counter()
    with path.open(encoding="utf-8") as stream:
        first = stream.readline()
        if not first:
            raise RuntimeError("archive JSONL is empty")
        manifest = json.loads(first)
        if manifest.get("type") != "manifest" or manifest.get("format_version") != 1:
            raise RuntimeError("unsupported archive manifest")
        for line in stream:
            record = json.loads(line)
            if record.get("type") != "raw_record":
                raise RuntimeError("unexpected archive record type")
            expected = hashlib.sha256(canonical_json(record["payload"])).hexdigest()
            if record.get("payload_sha256") != expected:
                raise RuntimeError("archive payload checksum mismatch")
            sources[str(record["source"])] += 1
            count += 1

    if count != int(manifest["record_count"]):
        raise RuntimeError("archive record count does not match the manifest")
    if dict(sorted(sources.items())) != manifest["source_counts"]:
        raise RuntimeError("archive source counts do not match the manifest")
    print(
        json.dumps(
            {
                "batch_id": manifest["batch_id"],
                "record_count": count,
                "source_counts": dict(sorted(sources.items())),
                "status": "ok",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
