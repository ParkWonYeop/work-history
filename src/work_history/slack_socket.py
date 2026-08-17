from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from work_history.collectors.slack import SlackCollector
from work_history.config import Settings
from work_history.services import get_cursor, set_cursor, upsert_normalized_batch

logger = logging.getLogger(__name__)


def _later_until(current: dict[str, Any] | None, candidate: datetime) -> str:
    value = (current or {}).get("until")
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo and parsed.astimezone(UTC) > candidate:
                return parsed.astimezone(UTC).isoformat()
        except ValueError:
            pass
    return candidate.astimezone(UTC).isoformat()


def run_slack_socket(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    settings.require_slack(socket_mode=True)
    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("Install work-history with the socket-mode dependency") from exc

    collector = SlackCollector(settings.slack_workspace_url, settings.slack_user_token)
    collector.refresh_context()
    client = SocketModeClient(
        app_token=settings.slack_app_token,
        web_client=WebClient(token=settings.slack_user_token),
        concurrency=1,
    )

    def acknowledge(request: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

    def process(_: SocketModeClient, request: SocketModeRequest) -> None:
        if request.type != "events_api":
            acknowledge(request)
            return
        try:
            event = request.payload.get("event") or {}
            channel_id = str(
                event.get("channel") or (event.get("item") or {}).get("channel") or ""
            )
            if channel_id and channel_id not in collector.conversations:
                collector.refresh_context(refresh_users=False)
            batch, event_at = collector.normalize_socket_event(request.payload)
            if batch.record_count:
                with session_factory() as session:
                    upsert_normalized_batch(session, batch, settings.raw_retention_days)
                    if event_at:
                        current = get_cursor(session, "slack", "socket")
                        set_cursor(
                            session,
                            "slack",
                            "socket",
                            {
                                "until": _later_until(current, event_at),
                                "last_success": datetime.now(UTC).isoformat(),
                            },
                        )
                    session.commit()
            acknowledge(request)
        except Exception:
            logger.exception("Slack Socket Mode event processing failed")

    client.socket_mode_request_listeners.append(process)
    try:
        client.connect()
        logger.info(
            "Slack Socket Mode connected team=%s conversations=%s",
            collector.team_id,
            len(collector.conversations),
        )
        while True:
            time.sleep(900)
            try:
                collector.refresh_context(refresh_users=False)
                logger.info(
                    "Slack membership refreshed conversations=%s",
                    len(collector.conversations),
                )
            except Exception:
                logger.exception("Slack membership refresh failed; retaining previous membership")
    finally:
        client.close()
        collector.close()
