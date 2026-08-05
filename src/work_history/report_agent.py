from __future__ import annotations

import argparse
import json
import os
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import keyring
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from work_history.security import b64url_decode, b64url_encode, sign_request

KEYRING_SERVICE = "com.workhistory.report-agent"
DEFAULT_CONFIG = Path.home() / "Library/Application Support/WorkHistoryReportAgent/config.toml"
DEFAULT_PROMPT_VERSION = "work-history-report-v1"
DEFAULT_MODEL = "gpt-5.6-terra"


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _secret(device_id: str) -> str:
    value = os.getenv("WORK_HISTORY_REPORT_SIGNING_KEY")
    if value:
        return value
    value = keyring.get_password(KEYRING_SERVICE, f"{device_id}:signing-key")
    if not value:
        raise RuntimeError("missing report-agent signing key in macOS Keychain")
    return value


def _store_secret(device_id: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, f"{device_id}:signing-key", value)


class ReportServerClient:
    def __init__(self, server_url: str, device_id: str, private_key: Ed25519PrivateKey) -> None:
        self.device_id = device_id
        self.private_key = private_key
        self.client = httpx.Client(
            base_url=server_url.rstrip("/"),
            timeout=httpx.Timeout(120.0, connect=10.0),
            headers={"User-Agent": "work-history-report-agent/0.1"},
        )

    def close(self) -> None:
        self.client.close()

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        nonce = str(uuid.uuid4())
        return {
            "Content-Type": "application/json",
            "X-WorkHistory-Device": self.device_id,
            "X-WorkHistory-Timestamp": timestamp,
            "X-WorkHistory-Nonce": nonce,
            "X-WorkHistory-Signature": sign_request(
                self.private_key,
                method,
                path,
                timestamp,
                nonce,
                body,
            ),
        }

    @staticmethod
    def _body(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._body(payload)
        response = self.client.post(path, content=body, headers=self._headers("POST", path, body))
        response.raise_for_status()
        return response.json()

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._body(payload)
        response = self.client.put(path, content=body, headers=self._headers("PUT", path, body))
        response.raise_for_status()
        return response.json()


def _client(config_path: Path) -> ReportServerClient:
    config = _load_config(config_path)
    device_id = str(config["device_id"])
    private = Ed25519PrivateKey.from_private_bytes(b64url_decode(_secret(device_id)))
    return ReportServerClient(str(config["server_url"]), device_id, private)


def _write_json(value: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
        output.chmod(0o600)
    else:
        print(serialized)


def initialize(config_path: Path, server_url: str, device_id: str) -> None:
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    _store_secret(device_id, b64url_encode(raw_private))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"device_id = {json.dumps(device_id)}\nserver_url = {json.dumps(server_url.rstrip('/'))}\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    print(b64url_encode(raw_public))


def _context_status(context: dict[str, Any]) -> str:
    return "final" if context.get("all_sources_fresh") else "partial"


def upload_document(
    client: ReportServerClient,
    cadence: str,
    period: str,
    kind: str,
    title: str,
    markdown: str,
    context: dict[str, Any],
    prompt_version: str,
    generator_model: str,
) -> dict[str, Any]:
    path = f"/v1/reports/{cadence}/{period}/{kind}"
    return client.put(
        path,
        {
            "status": _context_status(context),
            "title": title,
            "markdown": markdown,
            "source_snapshot": context.get("source_snapshot", {}),
            "source_event_counts": context.get("source_event_counts", {}),
            "prompt_version": prompt_version,
            "generator_model": generator_model,
        },
    )


def upload_empty_documents(
    client: ReportServerClient,
    period: str,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    source_lines = "\n".join(
        f"- {source}: {details.get('status', 'unknown')}"
        for source, details in context.get("source_snapshot", {}).items()
    )
    work_title = f"{period} 업무 보고서"
    feedback_title = f"{period} 업무 피드백"
    work_markdown = (
        f"# {work_title}\n\n"
        "## 요약\n\n수집된 업무 활동이 없습니다. 휴일, 휴가 또는 수집 지연일 수 있습니다.\n\n"
        "## 시간순 활동\n\n- 기록 없음\n\n"
        "## 업무별 상세\n\n- 평가할 업무 기록 없음\n\n"
        "## 결정·협업\n\n- 기록 없음\n\n"
        "## 장애·미해결\n\n- 기록 없음\n\n"
        "## 다음 작업\n\n- 다음 업무일 기록에서 확인\n\n"
        f"## 데이터 완전성\n\n{source_lines or '- 동기화 정보 없음'}\n"
    )
    feedback_markdown = (
        f"# {feedback_title}\n\n"
        "## 근거 기반 총평\n\n업무 활동 데이터가 없어 평가를 보류합니다.\n\n"
        "## 잘한 점\n\n- 판단할 근거 없음\n\n"
        "## 병목·개선점\n\n- 판단할 근거 없음\n\n"
        "## 협업·문서화\n\n- 판단할 근거 없음\n\n"
        "## 다음 근무일 행동\n\n"
        "1. 업무 기록 수집 상태 확인\n"
        "2. 필요한 경우 누락 활동 보완\n"
        "3. 다음 업무일 우선순위 확인\n\n"
        "## 판단 한계\n\n수집된 활동이 없으므로 성과나 업무 방식에 대한 추론을 하지 않았습니다.\n"
    )
    return [
        upload_document(
            client,
            "daily",
            period,
            "work_report",
            work_title,
            work_markdown,
            context,
            "empty-day-v1",
            "deterministic-template",
        ),
        upload_document(
            client,
            "daily",
            period,
            "feedback",
            feedback_title,
            feedback_markdown,
            context,
            "empty-day-v1",
            "deterministic-template",
        ),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Signed work-history report API client")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--server-url", required=True)
    init.add_argument("--device-id", default="codex-report-agent")

    context = sub.add_parser("context")
    context.add_argument("--cadence", choices=["daily", "monthly"], required=True)
    context.add_argument("--period", required=True)
    context.add_argument("--output", type=Path)

    missing = sub.add_parser("missing")
    missing.add_argument("--cadence", choices=["daily", "monthly"], required=True)
    missing.add_argument("--from", dest="from_date", required=True)
    missing.add_argument("--to", dest="to_date", required=True)
    missing.add_argument("--exclude-partial", action="store_true")
    missing.add_argument("--output", type=Path)

    upload = sub.add_parser("upload")
    upload.add_argument("--cadence", choices=["daily", "monthly"], required=True)
    upload.add_argument("--period", required=True)
    upload.add_argument("--kind", choices=["work_report", "feedback"], required=True)
    upload.add_argument("--title", required=True)
    upload.add_argument("--markdown-file", type=Path, required=True)
    upload.add_argument("--context-file", type=Path, required=True)
    upload.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    upload.add_argument("--generator-model", default=DEFAULT_MODEL)

    empty = sub.add_parser("upload-empty")
    empty.add_argument("--period", required=True)
    empty.add_argument("--context-file", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "init":
        initialize(args.config, args.server_url, args.device_id)
        return

    client = _client(args.config)
    try:
        if args.command == "context":
            result = client.post(
                "/v1/report-agent/context",
                {"cadence": args.cadence, "period": args.period},
            )
            _write_json(result, args.output)
            return
        if args.command == "missing":
            result = client.post(
                "/v1/report-agent/missing",
                {
                    "cadence": args.cadence,
                    "from": args.from_date,
                    "to": args.to_date,
                    "include_partial": not args.exclude_partial,
                },
            )
            _write_json(result, args.output)
            return
        context = json.loads(args.context_file.read_text(encoding="utf-8"))
        if args.command == "upload":
            result = upload_document(
                client,
                args.cadence,
                args.period,
                args.kind,
                args.title,
                args.markdown_file.read_text(encoding="utf-8"),
                context,
                args.prompt_version,
                args.generator_model,
            )
            _write_json(result, None)
            return
        if args.command == "upload-empty":
            if int(context.get("activity_count", -1)) != 0:
                raise RuntimeError("upload-empty requires a context with activity_count=0")
            _write_json({"items": upload_empty_documents(client, args.period, context)}, None)
            return
    finally:
        client.close()


if __name__ == "__main__":
    main()
