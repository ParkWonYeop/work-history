from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _read_secret(name: str, default: str = "") -> str:
    file_value = os.getenv(f"{name}_FILE")
    if file_value:
        return Path(file_value).read_text(encoding="utf-8").strip()
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    database_url: str
    read_api_token: str
    atlassian_site_url: str
    atlassian_email: str
    atlassian_api_token: str
    public_base_url: str
    raw_retention_days: int = 180
    signature_max_age_seconds: int = 300
    ingest_max_uncompressed_bytes: int = 5 * 1024 * 1024
    default_timezone: str = "Asia/Seoul"
    slack_workspace_url: str = ""
    slack_app_id: str = ""
    slack_user_token: str = ""
    slack_app_token: str = ""
    slack_history_start: str = "2026-04-01"
    raw_archive_dir: str = "/var/lib/work-history/raw-archive"
    raw_archive_age_recipient: str = ""
    raw_archive_r2_endpoint: str = ""
    raw_archive_r2_bucket: str = ""
    raw_archive_r2_prefix: str = "raw/v1"
    raw_archive_r2_access_key_id: str = ""
    raw_archive_r2_secret_access_key: str = ""
    raw_archive_lookahead_days: int = 7
    raw_archive_batch_size: int = 5000

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "sqlite+pysqlite:///./work-history.db",
            ),
            read_api_token=_read_secret("READ_API_TOKEN"),
            atlassian_site_url=os.getenv("ATLASSIAN_SITE_URL", "").rstrip("/"),
            atlassian_email=os.getenv("ATLASSIAN_EMAIL", ""),
            atlassian_api_token=_read_secret("ATLASSIAN_API_TOKEN"),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
            raw_retention_days=int(os.getenv("RAW_RETENTION_DAYS", "180")),
            signature_max_age_seconds=int(os.getenv("SIGNATURE_MAX_AGE_SECONDS", "300")),
            ingest_max_uncompressed_bytes=int(
                os.getenv("INGEST_MAX_UNCOMPRESSED_BYTES", str(5 * 1024 * 1024))
            ),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Asia/Seoul"),
            slack_workspace_url=os.getenv("SLACK_WORKSPACE_URL", "").rstrip("/"),
            slack_app_id=os.getenv("SLACK_APP_ID", ""),
            slack_user_token=_read_secret("SLACK_USER_TOKEN"),
            slack_app_token=_read_secret("SLACK_APP_TOKEN"),
            slack_history_start=os.getenv("SLACK_HISTORY_START", "2026-04-01"),
            raw_archive_dir=os.getenv(
                "RAW_ARCHIVE_DIR", "/var/lib/work-history/raw-archive"
            ),
            raw_archive_age_recipient=os.getenv("RAW_ARCHIVE_AGE_RECIPIENT", "").strip(),
            raw_archive_r2_endpoint=os.getenv("RAW_ARCHIVE_R2_ENDPOINT", "").rstrip("/"),
            raw_archive_r2_bucket=os.getenv("RAW_ARCHIVE_R2_BUCKET", "").strip(),
            raw_archive_r2_prefix=os.getenv("RAW_ARCHIVE_R2_PREFIX", "raw/v1").strip("/"),
            raw_archive_r2_access_key_id=_read_secret(
                "RAW_ARCHIVE_R2_ACCESS_KEY_ID"
            ),
            raw_archive_r2_secret_access_key=_read_secret(
                "RAW_ARCHIVE_R2_SECRET_ACCESS_KEY"
            ),
            raw_archive_lookahead_days=int(
                os.getenv("RAW_ARCHIVE_LOOKAHEAD_DAYS", "7")
            ),
            raw_archive_batch_size=int(os.getenv("RAW_ARCHIVE_BATCH_SIZE", "5000")),
        )

    def require_atlassian(self) -> None:
        missing = [
            name
            for name, value in (
                ("ATLASSIAN_SITE_URL", self.atlassian_site_url),
                ("ATLASSIAN_EMAIL", self.atlassian_email),
                ("ATLASSIAN_API_TOKEN", self.atlassian_api_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Atlassian settings: {', '.join(missing)}")

    def require_slack(self, *, socket_mode: bool = False) -> None:
        required = [
            ("SLACK_WORKSPACE_URL", self.slack_workspace_url),
            ("SLACK_USER_TOKEN", self.slack_user_token),
        ]
        if socket_mode:
            required.append(("SLACK_APP_TOKEN", self.slack_app_token))
        missing = [name for name, value in required if not value]
        if missing:
            raise RuntimeError(f"Missing Slack settings: {', '.join(missing)}")

    def require_raw_archive(self) -> None:
        required = (
            ("RAW_ARCHIVE_AGE_RECIPIENT", self.raw_archive_age_recipient),
            ("RAW_ARCHIVE_R2_ENDPOINT", self.raw_archive_r2_endpoint),
            ("RAW_ARCHIVE_R2_BUCKET", self.raw_archive_r2_bucket),
            ("RAW_ARCHIVE_R2_ACCESS_KEY_ID", self.raw_archive_r2_access_key_id),
            ("RAW_ARCHIVE_R2_SECRET_ACCESS_KEY", self.raw_archive_r2_secret_access_key),
        )
        missing = [name for name, value in required if not value]
        if missing:
            raise RuntimeError(f"Missing raw archive settings: {', '.join(missing)}")
        if not self.raw_archive_age_recipient.startswith("age1"):
            raise RuntimeError("RAW_ARCHIVE_AGE_RECIPIENT must be an age X25519 recipient")
        if not self.raw_archive_r2_endpoint.startswith("https://"):
            raise RuntimeError("RAW_ARCHIVE_R2_ENDPOINT must use HTTPS")
        prefix_parts = self.raw_archive_r2_prefix.split("/")
        if not self.raw_archive_r2_prefix or any(
            part in {"", ".", ".."} for part in prefix_parts
        ):
            raise RuntimeError("RAW_ARCHIVE_R2_PREFIX contains an unsafe path component")
        if self.raw_archive_batch_size < 1 or self.raw_archive_batch_size > 50_000:
            raise RuntimeError("RAW_ARCHIVE_BATCH_SIZE must be between 1 and 50000")
        if self.raw_archive_lookahead_days < 1 or self.raw_archive_lookahead_days > 31:
            raise RuntimeError("RAW_ARCHIVE_LOOKAHEAD_DAYS must be between 1 and 31")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
