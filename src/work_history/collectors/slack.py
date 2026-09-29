from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from work_history.collectors.common import ApiClient, in_window
from work_history.schemas import (
    ActivityRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    IdentityRecord,
    NormalizedBatch,
    RawRecordInput,
)

logger = logging.getLogger(__name__)

# ponytail: fixed lookback; replies to threads older than this still rely on Socket Mode.
THREAD_REPLY_LOOKBACK = timedelta(days=30)


class SlackApiError(RuntimeError):
    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"Slack {method} failed: {error}")
        self.method = method
        self.error = error


def slack_ts_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OSError):
        return None


def _nested_text(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, list):
        for item in value:
            result.extend(_nested_text(item))
    elif isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            result.append(text.strip())
        elif isinstance(text, dict):
            result.extend(_nested_text(text))
        for key, item in value.items():
            if key != "text":
                result.extend(_nested_text(item))
    return result


def message_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    primary = message.get("text")
    if isinstance(primary, str) and primary.strip():
        parts.append(primary.strip())
    for field in ("blocks", "attachments"):
        parts.extend(_nested_text(message.get(field, [])))
    for file_item in message.get("files") or []:
        title = file_item.get("title") or file_item.get("name")
        if title:
            parts.append(f"[파일] {title}")
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part and part not in seen:
            unique.append(part)
            seen.add(part)
    return "\n".join(unique)


class SlackCollector:
    """Collect only conversations visible to, and joined by, the user token owner."""

    def __init__(self, workspace_url: str, user_token: str) -> None:
        client = httpx.Client(
            base_url="https://slack.com/api",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {user_token}",
            },
            timeout=httpx.Timeout(45.0, connect=10.0),
        )
        self.workspace_url = workspace_url.rstrip("/")
        self.api = ApiClient(client)
        self.self_id = ""
        self.team_id = ""
        self.users: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self.api.client.close()

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        payload = self.api.post_json(f"/{method}", data=params)
        if not payload.get("ok"):
            raise SlackApiError(method, str(payload.get("error") or "unknown_error"))
        return payload

    def _pages(
        self,
        method: str,
        item_key: str,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        cursor = ""
        while True:
            payload = self._call(method, **params, cursor=cursor)
            yield from payload.get(item_key) or []
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break

    def refresh_context(self, *, refresh_users: bool = True) -> None:
        if not self.self_id or not self.team_id:
            auth = self._call("auth.test")
            self.self_id = str(auth["user_id"])
            self.team_id = str(auth["team_id"])
        users = self.users
        if refresh_users or not users:
            users = {
                str(item["id"]): item
                for item in self._pages("users.list", "members", limit=200)
                if item.get("id")
            }
        conversations: dict[str, dict[str, Any]] = {}
        for item in self._pages(
            "users.conversations",
            "channels",
            types="public_channel,private_channel,mpim,im",
            exclude_archived="false",
            limit=200,
        ):
            conversation_id = str(item.get("id") or "")
            if not conversation_id:
                continue
            conversations[conversation_id] = item
        self.users = users
        self.conversations = conversations

    def _identity(self, user_id: str) -> IdentityRecord:
        user = self.users.get(user_id, {})
        profile = user.get("profile") or {}
        return IdentityRecord(
            source="slack",
            remote_id=user_id,
            username=user.get("name"),
            display_name=(
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            ),
            email=profile.get("email"),
            is_self=user_id == self.self_id,
            raw=user,
        )

    def _user_name(self, user_id: str | None, message: dict[str, Any]) -> str:
        if user_id:
            identity = self._identity(user_id)
            return identity.display_name or identity.username or user_id
        bot_profile = message.get("bot_profile") or {}
        return str(bot_profile.get("name") or message.get("username") or "시스템")

    def _conversation_name(self, conversation: dict[str, Any]) -> str:
        if conversation.get("is_im"):
            other_id = str(conversation.get("user") or "")
            return f"DM: {self._user_name(other_id, {})}"
        if conversation.get("is_mpim"):
            members = [
                self._user_name(str(item), {})
                for item in conversation.get("members") or []
                if str(item) != self.self_id
            ]
            return "그룹 DM: " + (", ".join(members) or str(conversation.get("name") or ""))
        name = str(conversation.get("name") or conversation.get("id") or "")
        return f"#{name}"

    def _conversation_type(self, conversation: dict[str, Any]) -> str:
        if conversation.get("is_im"):
            return "im"
        if conversation.get("is_mpim"):
            return "mpim"
        if conversation.get("is_private"):
            return "private_channel"
        return "public_channel"

    def _message_url(self, channel_id: str, timestamp: str) -> str:
        return f"{self.workspace_url}/archives/{channel_id}/p{timestamp.replace('.', '')}"

    def _remote_id(self, channel_id: str, timestamp: str) -> str:
        return f"{self.team_id}:{channel_id}:{timestamp}"

    def _append_identity(
        self,
        batch: NormalizedBatch,
        identity_ids: set[str],
        user_id: str | None,
    ) -> None:
        if not user_id or user_id in identity_ids:
            return
        batch.identities.append(self._identity(user_id))
        identity_ids.add(user_id)

    def _append_message(
        self,
        batch: NormalizedBatch,
        identity_ids: set[str],
        conversation: dict[str, Any],
        message: dict[str, Any],
        collected_at: datetime,
        *,
        include_post_event: bool = True,
        action: str | None = None,
        action_at: datetime | None = None,
        event_key_suffix: str | None = None,
        deleted: bool = False,
    ) -> None:
        channel_id = str(conversation["id"])
        timestamp = str(message.get("ts") or message.get("deleted_ts") or "")
        created_at = slack_ts_to_datetime(timestamp)
        if not timestamp or created_at is None:
            return
        actor_id = str(message.get("user") or "") or None
        self._append_identity(batch, identity_ids, actor_id)
        remote_id = self._remote_id(channel_id, timestamp)
        conversation_name = self._conversation_name(conversation)
        author_name = self._user_name(actor_id, message)
        title = f"{conversation_name} · {author_name}"
        url = self._message_url(channel_id, timestamp)
        edited_timestamp = str((message.get("edited") or {}).get("ts") or "")
        updated_at = slack_ts_to_datetime(edited_timestamp) or created_at
        body = None if deleted else message_text(message)
        thread_ts = str(message.get("thread_ts") or "")
        kind = "slack_thread_reply" if thread_ts and thread_ts != timestamp else "slack_message"
        state = "deleted" if deleted else "active"
        raw = {
            **message,
            "conversation": {
                "id": channel_id,
                "name": conversation_name,
                "type": self._conversation_type(conversation),
            },
        }
        batch.artifacts.append(
            ArtifactRecord(
                source="slack",
                remote_id=remote_id,
                kind=kind,
                title=title,
                body_text=body,
                state=state,
                namespace=conversation_name,
                url=url,
                created_at=created_at,
                updated_at=action_at or updated_at,
                raw=raw,
            )
        )
        version_timestamp = action_at.isoformat() if action_at else (edited_timestamp or timestamp)
        batch.versions.append(
            ArtifactVersionRecord(
                source="slack",
                artifact_remote_id=remote_id,
                remote_version_id=(
                    f"deleted:{version_timestamp}" if deleted else f"message:{version_timestamp}"
                ),
                author_remote_id=actor_id,
                body_text=body,
                created_at=action_at or updated_at,
                raw=raw,
            )
        )
        changes = {
            "channel_id": channel_id,
            "conversation": conversation_name,
            "conversation_type": self._conversation_type(conversation),
            "thread_ts": thread_ts or None,
            "subtype": message.get("subtype"),
        }
        if include_post_event:
            post_action = "thread_replied" if kind == "slack_thread_reply" else "message_posted"
            batch.events.append(
                ActivityRecord(
                    source="slack",
                    event_key=f"message:{channel_id}:{timestamp}:posted",
                    kind=kind,
                    action=post_action,
                    occurred_at=created_at,
                    actor_remote_id=actor_id,
                    actor_is_self=actor_id == self.self_id,
                    artifact_remote_id=remote_id,
                    title=title,
                    changes=changes,
                    url=url,
                    raw=raw,
                )
            )
        if action and action_at:
            suffix = event_key_suffix or action_at.isoformat()
            batch.events.append(
                ActivityRecord(
                    source="slack",
                    event_key=f"message:{channel_id}:{timestamp}:{action}:{suffix}",
                    kind=kind,
                    action=action,
                    occurred_at=action_at,
                    actor_remote_id=actor_id,
                    actor_is_self=actor_id == self.self_id,
                    artifact_remote_id=remote_id,
                    title=title,
                    changes=changes,
                    url=url,
                    raw=raw,
                )
            )
        raw_version = version_timestamp.replace(":", "-")
        batch.raw_records.append(
            RawRecordInput(
                source="slack",
                record_key=f"message:{channel_id}:{timestamp}:{raw_version}",
                kind=kind,
                payload=raw,
                collected_at=collected_at,
            )
        )

    def _history(
        self,
        channel_id: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[dict[str, Any]]:
        yield from self._pages(
            "conversations.history",
            "messages",
            channel=channel_id,
            oldest=f"{start.timestamp():.6f}",
            latest=f"{end.timestamp():.6f}",
            inclusive="true",
            limit=200,
        )

    def _replies(
        self,
        channel_id: str,
        thread_ts: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[dict[str, Any]]:
        yield from self._pages(
            "conversations.replies",
            "messages",
            channel=channel_id,
            ts=thread_ts,
            oldest=f"{start.timestamp():.6f}",
            latest=f"{end.timestamp():.6f}",
            inclusive="true",
            limit=200,
        )

    def collect(self, start: datetime, end: datetime) -> NormalizedBatch:
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        self.refresh_context()
        batch = NormalizedBatch()
        identity_ids: set[str] = set()
        self._append_identity(batch, identity_ids, self.self_id)
        collected_at = datetime.now(UTC)
        for channel_id, conversation in sorted(self.conversations.items()):
            messages: dict[str, dict[str, Any]] = {}
            try:
                # conversations.history lists thread parents only by their own ts, so scan
                # back for older parents whose latest_reply lands inside the window.
                for message in self._history(channel_id, start - THREAD_REPLY_LOOKBACK, end):
                    timestamp = str(message.get("ts") or "")
                    if not timestamp:
                        continue
                    posted_in_window = in_window(slack_ts_to_datetime(timestamp), start, end)
                    if posted_in_window:
                        messages[timestamp] = message
                    latest_reply = slack_ts_to_datetime(str(message.get("latest_reply") or ""))
                    replied_in_window = latest_reply is not None and latest_reply >= start
                    if message.get("reply_count") and (posted_in_window or replied_in_window):
                        for reply in self._replies(channel_id, timestamp, start, end):
                            reply_timestamp = str(reply.get("ts") or "")
                            if reply_timestamp:
                                messages[reply_timestamp] = reply
            except SlackApiError as exc:
                if exc.error in {"channel_not_found", "not_in_channel", "is_archived"}:
                    logger.warning("Slack conversation became unavailable: %s", channel_id)
                    continue
                raise
            for timestamp in sorted(messages, key=float):
                message = messages[timestamp]
                occurred_at = slack_ts_to_datetime(timestamp)
                if not in_window(occurred_at, start, end):
                    continue
                self._append_message(
                    batch,
                    identity_ids,
                    conversation,
                    message,
                    collected_at,
                )
                edited_at = slack_ts_to_datetime(str((message.get("edited") or {}).get("ts") or ""))
                if edited_at and in_window(edited_at, start, end):
                    self._append_message(
                        batch,
                        identity_ids,
                        conversation,
                        message,
                        collected_at,
                        include_post_event=False,
                        action="message_edited",
                        action_at=edited_at,
                    )
        return batch

    def normalize_socket_event(
        self,
        payload: dict[str, Any],
    ) -> tuple[NormalizedBatch, datetime | None]:
        event = payload.get("event") or {}
        event_type = str(event.get("type") or "")
        event_id = str(payload.get("event_id") or event.get("event_ts") or "")
        event_at = slack_ts_to_datetime(str(event.get("event_ts") or event.get("ts") or ""))
        channel_id = str(
            event.get("channel") or (event.get("item") or {}).get("channel") or ""
        )
        conversation = self.conversations.get(channel_id)
        if not conversation:
            return NormalizedBatch(), event_at
        batch = NormalizedBatch()
        identity_ids: set[str] = set()
        collected_at = datetime.now(UTC)

        if event_type == "message":
            subtype = str(event.get("subtype") or "")
            if subtype == "message_changed":
                message = event.get("message") or {}
                previous = event.get("previous_message") or {}
                self._append_message(
                    batch,
                    identity_ids,
                    conversation,
                    previous,
                    collected_at,
                    include_post_event=False,
                )
                self._append_message(
                    batch,
                    identity_ids,
                    conversation,
                    message,
                    collected_at,
                    include_post_event=False,
                    action="message_edited",
                    action_at=event_at,
                    event_key_suffix=event_id,
                )
            elif subtype == "message_deleted":
                previous = dict(event.get("previous_message") or {})
                previous.setdefault("ts", event.get("deleted_ts"))
                self._append_message(
                    batch,
                    identity_ids,
                    conversation,
                    previous,
                    collected_at,
                    include_post_event=False,
                )
                self._append_message(
                    batch,
                    identity_ids,
                    conversation,
                    previous,
                    collected_at,
                    include_post_event=False,
                    action="message_deleted",
                    action_at=event_at,
                    event_key_suffix=event_id,
                    deleted=True,
                )
            else:
                self._append_message(
                    batch,
                    identity_ids,
                    conversation,
                    event,
                    collected_at,
                    event_key_suffix=event_id,
                )
        elif event_type in {"reaction_added", "reaction_removed"} and event_at:
            item = event.get("item") or {}
            timestamp = str(item.get("ts") or "")
            if not timestamp:
                return batch, event_at
            actor_id = str(event.get("user") or "") or None
            self._append_identity(batch, identity_ids, actor_id)
            remote_id = self._remote_id(channel_id, timestamp)
            reaction = str(event.get("reaction") or "")
            action = event_type
            batch.events.append(
                ActivityRecord(
                    source="slack",
                    event_key=f"reaction:{channel_id}:{timestamp}:{reaction}:{actor_id}:{action}:{event_id}",
                    kind="slack_reaction",
                    action=action,
                    occurred_at=event_at,
                    actor_remote_id=actor_id,
                    actor_is_self=actor_id == self.self_id,
                    artifact_remote_id=remote_id,
                    title=f"{self._conversation_name(conversation)} · :{reaction}:",
                    changes={
                        "channel_id": channel_id,
                        "reaction": reaction,
                        "message_ts": timestamp,
                    },
                    url=self._message_url(channel_id, timestamp),
                    raw=event,
                )
            )
        else:
            return batch, event_at

        if event_id:
            batch.raw_records.append(
                RawRecordInput(
                    source="slack",
                    record_key=f"socket-event:{event_id}",
                    kind=f"slack_{event_type or 'event'}",
                    payload=payload,
                    collected_at=collected_at,
                )
            )
        return batch, event_at
