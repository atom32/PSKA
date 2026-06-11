from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pska_core.enums import Visibility
from pska_core.models import ChannelIngestPayload


def conversation_to_payload(
    conversation: dict[str, Any],
    *,
    owner_user_id: str,
    space_id: str,
    visibility: Visibility = Visibility.PRIVATE,
    visible_team_ids: list[str] | None = None,
) -> ChannelIngestPayload:
    """Convert a conversation/session export into the standard channel ingest payload."""

    conversation_id = str(conversation.get("conversation_id") or conversation.get("session_id") or conversation["id"])
    messages = _messages(conversation.get("messages"))
    participants = _participants(conversation.get("participants"))
    transcript = _transcript(messages)
    title = str(conversation.get("title") or _default_title(messages) or f"Conversation {conversation_id}")
    created_at = str(conversation.get("created_at") or _first_message_time(messages) or "")
    captured_at = str(conversation.get("captured_at") or datetime.now(timezone.utc).isoformat())

    return ChannelIngestPayload(
        schema_version="pska.channel_ingest.v1",
        source_channel=str(conversation.get("source_channel") or "conversation"),
        record_type="conversation",
        source_id=conversation_id,
        owner_user_id=owner_user_id,
        space_id=space_id,
        visibility=visibility,
        visible_team_ids=visible_team_ids or [],
        url=conversation.get("url"),
        title=title,
        author={"participants": participants},
        content={
            "text": transcript,
            "messages": messages,
            "participants": participants,
            "tool_calls": conversation.get("tool_calls") or [],
            "citations": conversation.get("citations") or [],
        },
        created_at=created_at,
        captured_at=captured_at,
        raw_paths=dict(conversation.get("raw_paths") or {}),
        extra={
            "conversation_id": conversation_id,
            "session_id": conversation.get("session_id"),
            "message_count": len(messages),
        },
    )


def _messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("conversation requires a non-empty messages list")
    messages: list[dict[str, Any]] = []
    for index, message in enumerate(value):
        if not isinstance(message, dict):
            raise ValueError("conversation messages must be objects")
        message_id = str(message.get("message_id") or message.get("id") or f"msg_{index + 1}")
        role = str(message.get("role") or message.get("sender_role") or "user")
        content = str(message.get("content") or message.get("text") or "")
        if not content:
            continue
        messages.append(
            {
                "message_id": message_id,
                "role": role,
                "participant_id": message.get("participant_id"),
                "participant_name": message.get("participant_name"),
                "content": content,
                "created_at": message.get("created_at") or message.get("timestamp"),
                "tool_calls": message.get("tool_calls") or [],
                "citations": message.get("citations") or [],
            }
        )
    if not messages:
        raise ValueError("conversation requires at least one non-empty message")
    return messages


def _participants(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        speaker = message.get("participant_name") or message.get("participant_id") or message["role"]
        timestamp = f" [{message['created_at']}]" if message.get("created_at") else ""
        lines.append(f"{message['message_id']} {speaker}{timestamp}: {message['content']}")
    return "\n".join(lines)


def _first_message_time(messages: list[dict[str, Any]]) -> str | None:
    for message in messages:
        if message.get("created_at"):
            return str(message["created_at"])
    return None


def _default_title(messages: list[dict[str, Any]]) -> str:
    first = messages[0]["content"].replace("\n", " ").strip()
    return first[:80]
