from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pska_core.adapters.conversation import conversation_to_payload
from pska_core.enums import Visibility
from pska_core.ingest import IngestService
from pska_core.models import SourceItem, SourceRef
from pska_core.serde import to_jsonable
from pska_core.store import KnowledgeStore


def capture_agent_conversation(
    store: KnowledgeStore,
    *,
    owner_user_id: str,
    purpose: str,
    prompt: str,
    answer: str,
    source_refs: list[dict[str, Any]] | None = None,
    trace_summary: dict[str, Any] | None = None,
    represented_user_id: str | None = None,
    title: str | None = None,
    source_channel: str = "pska_agent",
    conversation_id: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> SourceItem:
    refs = _normalize_source_refs(source_refs or citations or [])
    now = datetime.now(timezone.utc).isoformat()
    payload = conversation_to_payload(
        {
            "id": conversation_id or f"pska_agent_{purpose}_{uuid4().hex}",
            "source_channel": source_channel,
            "title": title or f"PSKA agent capture: {purpose}",
            "captured_at": now,
            "participants": [
                {"participant_id": represented_user_id or owner_user_id, "name": represented_user_id or owner_user_id},
                {"participant_id": "pska_agent", "name": "PSKA Agent"},
            ],
            "messages": [
                {
                    "id": "msg_user_prompt",
                    "role": "user",
                    "participant_id": represented_user_id or owner_user_id,
                    "content": prompt,
                    "created_at": now,
                },
                {
                    "id": "msg_agent_answer",
                    "role": "assistant",
                    "participant_id": "pska_agent",
                    "content": answer,
                    "created_at": now,
                    "citations": refs,
                },
            ],
            "citations": refs,
            "tool_calls": tool_calls or [],
            "trace_summary": trace_summary or {},
            "extra": {
                "purpose": purpose,
                "represented_user_id": represented_user_id or owner_user_id,
                "source_refs": refs,
                "trace_summary": trace_summary or {},
            },
        },
        owner_user_id=owner_user_id,
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
    )
    return IngestService(store).ingest_channel_payload(payload)


def _normalize_source_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = set(SourceRef.__dataclass_fields__)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        value = {key: item for key, item in to_jsonable(ref).items() if key in allowed and item}
        marker = tuple(sorted(value.items()))
        if value and marker not in seen:
            seen.add(marker)
            normalized.append(value)
    return normalized
