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
    raw_retention_days: int = 30
    signature_max_age_seconds: int = 300
    ingest_max_uncompressed_bytes: int = 5 * 1024 * 1024
    default_timezone: str = "Asia/Seoul"

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
            raw_retention_days=int(os.getenv("RAW_RETENTION_DAYS", "30")),
            signature_max_age_seconds=int(os.getenv("SIGNATURE_MAX_AGE_SECONDS", "300")),
            ingest_max_uncompressed_bytes=int(
                os.getenv("INGEST_MAX_UNCOMPRESSED_BYTES", str(5 * 1024 * 1024))
            ),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Asia/Seoul"),
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
