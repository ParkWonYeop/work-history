from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        for pattern in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                result = datetime.strptime(value, pattern)
                break
            except ValueError:
                continue
        else:
            raise
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def in_window(value: datetime | None, start: datetime, end: datetime) -> bool:
    return value is not None and start <= value < end


def adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(adf_to_text(item) for item in value)
    if not isinstance(value, dict):
        return str(value)
    node_type = value.get("type")
    text = value.get("text", "")
    children = "".join(adf_to_text(item) for item in value.get("content", []))
    if node_type in {"paragraph", "heading", "blockquote", "listItem"}:
        return f"{text}{children}\n"
    if node_type == "hardBreak":
        return "\n"
    if node_type in {"bulletList", "orderedList"}:
        return f"{children}\n"
    return f"{text}{children}"


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text("\n", strip=True)


class ApiClient:
    def __init__(self, client: httpx.Client, max_attempts: int = 6) -> None:
        self.client = client
        self.max_attempts = max_attempts

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_attempts):
            response = self.client.request(method, url, **kwargs)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                return response
            if attempt == self.max_attempts - 1:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2**attempt
            except ValueError:
                delay = 2**attempt
            delay = min(delay, 60.0) + random.uniform(0, 0.25)
            logger.warning(
                "API request retry status=%s attempt=%s delay=%.2f",
                response.status_code,
                attempt + 1,
                delay,
            )
            time.sleep(delay)
        raise RuntimeError("unreachable")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs).json()
