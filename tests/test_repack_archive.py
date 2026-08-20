from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "deploy" / "macos" / "repack-archive.py"
SPEC = importlib.util.spec_from_file_location("repack_archive", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _record(key: str, value: int) -> dict:
    payload = {"value": value}
    return {
        "collected_at": "2026-08-20T00:00:00+00:00",
        "expires_at": "2027-02-16T00:00:00+00:00",
        "kind": "event",
        "payload": payload,
        "payload_sha256": MODULE.hashlib.sha256(MODULE.canonical_json(payload)).hexdigest(),
        "record_key": key,
        "source": "gitlab",
        "type": "raw_record",
    }


def test_repack_removes_only_exact_fingerprints(tmp_path: Path) -> None:
    records = [_record("event:1", 1), _record("event:2", 2)]
    manifest = {
        "batch_id": "old",
        "created_at": "2026-08-20T00:00:00+00:00",
        "format_version": 1,
        "record_count": 2,
        "source_counts": {"gitlab": 2},
        "type": "manifest",
    }
    source = tmp_path / "source.jsonl"
    source.write_bytes(
        b"\n".join(MODULE.canonical_json(value) for value in [manifest, *records]) + b"\n"
    )
    exclusions = tmp_path / "exclude.json"
    exclusions.write_text(
        json.dumps(
            [
                {
                    key: records[0][key]
                    for key in ("source", "record_key", "collected_at", "payload_sha256")
                }
            ]
        )
    )
    output = tmp_path / "clean.jsonl"

    result = MODULE.repack(
        source,
        exclusions,
        output,
        "new",
        "2026-08-20T01:00:00+00:00",
    )

    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert result["excluded"] == 1
    assert result["records"] == 1
    assert lines[0]["batch_id"] == "new"
    assert lines[0]["record_count"] == 1
    assert lines[1]["record_key"] == "event:2"
