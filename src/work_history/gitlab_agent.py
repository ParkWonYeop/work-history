from __future__ import annotations

import argparse
import getpass
import gzip
import json
import logging
import os
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import keyring
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from work_history.collectors.gitlab import GitLabCollector
from work_history.schemas import GitLabIngestBatch, NormalizedBatch
from work_history.security import b64url_decode, b64url_encode, sign_request

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "com.workhistory.gitlab-agent"
DEFAULT_CONFIG = Path.home() / "Library/Application Support/WorkHistoryAgent/config.toml"
SCHEDULE_TIMEZONE = ZoneInfo("Asia/Seoul")
SCHEDULE_FIRST_HOUR = 9
SCHEDULE_LAST_HOUR = 17
MAX_CATCH_UP_WINDOWS = 64
RECORD_COLLECTIONS = (
    "identities",
    "artifacts",
    "versions",
    "events",
    "raw_records",
    "unavailable_artifacts",
)


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _secret(device_id: str, name: str) -> str:
    env_name = f"WORK_HISTORY_{name.upper().replace('-', '_')}"
    if os.getenv(env_name):
        return os.environ[env_name]
    value = keyring.get_password(KEYRING_SERVICE, f"{device_id}:{name}")
    if not value:
        raise RuntimeError(f"missing Keychain secret: {name}")
    return value


def _store_secret(device_id: str, name: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, f"{device_id}:{name}", value)


class SignedServerClient:
    def __init__(self, server_url: str, device_id: str, private_key: Ed25519PrivateKey) -> None:
        self.device_id = device_id
        self.private_key = private_key
        self.client = httpx.Client(
            base_url=server_url.rstrip("/"),
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"User-Agent": "work-history-agent/0.1"},
        )

    def close(self) -> None:
        self.client.close()

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        nonce = str(uuid.uuid4())
        signature = sign_request(
            self.private_key,
            method,
            path,
            timestamp,
            nonce,
            body,
        )
        return {
            "X-WorkHistory-Device": self.device_id,
            "X-WorkHistory-Timestamp": timestamp,
            "X-WorkHistory-Nonce": nonce,
            "X-WorkHistory-Signature": signature,
        }

    def checkpoint(self) -> dict[str, Any] | None:
        path = "/v1/ingest/gitlab/checkpoint"
        response = self.client.get(path, headers=self._headers("GET", path, b""))
        response.raise_for_status()
        return response.json().get("checkpoint")

    def upload(self, batch: GitLabIngestBatch) -> dict[str, Any]:
        path = "/v1/ingest/gitlab/batches"
        body = batch.model_dump_json(exclude_none=True).encode("utf-8")
        compressed = gzip.compress(body, compresslevel=6, mtime=0)
        headers = self._headers("POST", path, compressed)
        headers.update(
            {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            }
        )
        response = self.client.post(path, content=compressed, headers=headers)
        response.raise_for_status()
        return response.json()


def _private_key(device_id: str) -> Ed25519PrivateKey:
    encoded = _secret(device_id, "signing-key")
    return Ed25519PrivateKey.from_private_bytes(b64url_decode(encoded))


def _choose_window(
    checkpoint: dict[str, Any] | None,
    history_start: datetime | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = now or datetime.now(UTC)
    if not checkpoint or not checkpoint.get("until"):
        start = history_start or now - timedelta(days=365)
        return start, min(start + timedelta(days=7), now)
    previous = datetime.fromisoformat(str(checkpoint["until"]).replace("Z", "+00:00"))
    previous = previous.astimezone(UTC)
    if history_start is not None:
        previous = max(previous, history_start)
    if previous < now - timedelta(hours=48):
        return previous, min(previous + timedelta(days=7), now)
    return max(previous - timedelta(hours=48), now - timedelta(hours=48)), now


def _checkpoint_time(checkpoint: dict[str, Any] | None, key: str) -> datetime | None:
    if not checkpoint or not checkpoint.get(key):
        return None
    try:
        parsed = datetime.fromisoformat(str(checkpoint[key]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _configured_history_start(config: dict[str, Any]) -> datetime | None:
    value = config.get("history_start")
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeError("history_start must include a timezone")
    return parsed.astimezone(UTC)


def _within_scheduled_window(now: datetime) -> bool:
    local_now = now.astimezone(SCHEDULE_TIMEZONE)
    return (
        local_now.weekday() < 5
        and SCHEDULE_FIRST_HOUR <= local_now.hour <= SCHEDULE_LAST_HOUR
    )


def _scheduled_run_due(checkpoint: dict[str, Any] | None, now: datetime) -> bool:
    if not _within_scheduled_window(now):
        return False
    until = _checkpoint_time(checkpoint, "until")
    if until is None or until < now - timedelta(hours=48):
        return True
    last_success = _checkpoint_time(checkpoint, "last_success")
    if last_success is None:
        return True
    return last_success.astimezone(SCHEDULE_TIMEZONE).date() < now.astimezone(
        SCHEDULE_TIMEZONE
    ).date()


def _split_batch(
    normalized: NormalizedBatch,
    device_id: str,
    source_instance: str,
    checkpoint: dict[str, Any],
    batch_size: int = 400,
) -> list[GitLabIngestBatch]:
    records: list[tuple[str, Any]] = []
    for collection in RECORD_COLLECTIONS:
        records.extend((collection, item) for item in getattr(normalized, collection))
    if not records:
        chunks: list[list[tuple[str, Any]]] = [[]]
    else:
        chunks = [
            records[index : index + batch_size] for index in range(0, len(records), batch_size)
        ]
    output: list[GitLabIngestBatch] = []
    for index, chunk in enumerate(chunks):
        values: dict[str, list[Any]] = {name: [] for name in RECORD_COLLECTIONS}
        for collection, record in chunk:
            values[collection].append(record)
        fingerprint = json.dumps(
            {
                name: [item.model_dump(mode="json") for item in items]
                for name, items in values.items()
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        batch_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{device_id}:{source_instance}:{index}:"
                f"{json.dumps(checkpoint, sort_keys=True, separators=(',', ':'))}:"
                f"{fingerprint}",
            )
        )
        output.append(
            GitLabIngestBatch(
                batch_id=batch_id,
                source_instance=source_instance,
                collected_at=datetime.now(UTC),
                next_checkpoint=checkpoint if index == len(chunks) - 1 else None,
                **values,
            )
        )
    return output


def run_agent(
    config_path: Path,
    *,
    scheduled: bool = False,
    catch_up: bool = False,
) -> int:
    now = datetime.now(UTC)
    if scheduled and not _within_scheduled_window(now):
        logger.info("Outside the weekday 09:00-17:00 retry window; skipping")
        return 0
    config = _load_config(config_path)
    device_id = str(config["device_id"])
    server = SignedServerClient(
        str(config["server_url"]),
        device_id,
        _private_key(device_id),
    )
    gitlab = GitLabCollector(str(config["gitlab_url"]), _secret(device_id, "gitlab-pat"))
    try:
        checkpoint = server.checkpoint()
        if scheduled and not _scheduled_run_due(checkpoint, now):
            logger.info("GitLab synchronization already succeeded today; skipping")
            return 0
        try:
            gitlab.check_connection()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            logger.info("GitLab is unreachable; VPN is probably disconnected")
            return 0
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                logger.info("GitLab gateway is unavailable; VPN is probably disconnected")
                return 0
            raise
        history_start = _configured_history_start(config)
        target_time = datetime.now(UTC)
        windows = 0
        while True:
            start, end = _choose_window(checkpoint, history_start, target_time)
            logger.info("Collecting GitLab activity from %s to %s", start, end)
            normalized = gitlab.collect(start, end)
            next_checkpoint = {
                "until": end.isoformat(),
                "last_success": datetime.now(UTC).isoformat(),
            }
            batches = _split_batch(
                normalized,
                device_id,
                str(config["gitlab_url"]),
                next_checkpoint,
            )
            accepted = 0
            for batch in batches:
                response = server.upload(batch)
                accepted += int(response.get("accepted", 0))
            windows += 1
            logger.info(
                "GitLab synchronization completed; accepted=%s batches=%s window=%s",
                accepted,
                len(batches),
                windows,
            )
            checkpoint = next_checkpoint
            if not catch_up or end >= target_time:
                break
            if windows >= MAX_CATCH_UP_WINDOWS:
                logger.warning(
                    "GitLab catch-up paused after %s windows; the next run will resume",
                    windows,
                )
                break
        return 0
    finally:
        gitlab.close()
        server.close()


def initialize(
    config_path: Path,
    server_url: str,
    gitlab_url: str,
    device_id: str,
    history_start: str | None = None,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    raw_private = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    raw_public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    pat = getpass.getpass("GitLab personal access token: ")
    if not pat:
        raise RuntimeError("GitLab token may not be empty")
    if history_start:
        _configured_history_start({"history_start": history_start})
    _store_secret(device_id, "signing-key", b64url_encode(raw_private))
    _store_secret(device_id, "gitlab-pat", pat)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_text = (
        f"device_id = {json.dumps(device_id)}\n"
        f"server_url = {json.dumps(server_url.rstrip('/'))}\n"
        f"gitlab_url = {json.dumps(gitlab_url.rstrip('/'))}\n"
    )
    if history_start:
        config_text += f"history_start = {json.dumps(history_start)}\n"
    config_path.write_text(config_text, encoding="utf-8")
    config_path.chmod(0o600)
    print("Register this public key on the server:")
    print(b64url_encode(raw_public))


def set_token(config_path: Path) -> None:
    config = _load_config(config_path)
    token = getpass.getpass("New GitLab personal access token: ")
    if not token:
        raise RuntimeError("GitLab token may not be empty")
    _store_secret(str(config["device_id"]), "gitlab-pat", token)


def set_history_start(config_path: Path, history_start: str) -> None:
    _configured_history_start({"history_start": history_start})
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("history_start =")
    ]
    lines.append(f"history_start = {json.dumps(history_start)}")
    temporary = config_path.with_name(f"{config_path.name}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(config_path)


def show_public_key(config_path: Path) -> None:
    config = _load_config(config_path)
    private = _private_key(str(config["device_id"]))
    raw_public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    print(b64url_encode(raw_public))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitLab work-history agent")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init")
    init_parser.add_argument("--server-url", required=True)
    init_parser.add_argument("--gitlab-url", required=True)
    init_parser.add_argument("--device-id", default=f"mac-{uuid.uuid4().hex[:12]}")
    init_parser.add_argument("--history-start")
    sub.add_parser("run")
    sub.add_parser("catch-up")
    sub.add_parser("scheduled-run")
    sub.add_parser("set-token")
    history_parser = sub.add_parser("set-history-start")
    history_parser.add_argument("--history-start", required=True)
    sub.add_parser("public-key")
    return parser


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            initialize(
                args.config,
                args.server_url,
                args.gitlab_url,
                args.device_id,
                args.history_start,
            )
        elif args.command == "run":
            raise SystemExit(run_agent(args.config))
        elif args.command == "catch-up":
            raise SystemExit(run_agent(args.config, catch_up=True))
        elif args.command == "scheduled-run":
            raise SystemExit(run_agent(args.config, scheduled=True, catch_up=True))
        elif args.command == "set-token":
            set_token(args.config)
        elif args.command == "set-history-start":
            set_history_start(args.config, args.history_start)
        elif args.command == "public-key":
            show_public_key(args.config)
    except Exception as exc:
        logger.error("Agent failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
