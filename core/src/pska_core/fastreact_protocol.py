from __future__ import annotations

import json
from typing import Any


SCHEMA_VERSION = "fastreact.agent_event.v1"
TERMINAL_SSE_DATA = "[DONE]"


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    """Parse FastReAct SSE text into JSON event payloads."""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        data_lines = []
        event_name = None
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())

        if not data_lines:
            continue

        data = "\n".join(data_lines)
        if data == TERMINAL_SSE_DATA:
            events.append({"schema": SCHEMA_VERSION, "type": "done", "content": TERMINAL_SSE_DATA})
            continue

        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            event = {"schema": SCHEMA_VERSION, "type": "invalid_sse_json", "content": data}

        if isinstance(event, dict):
            if event_name and "type" not in event:
                event["type"] = event_name
            event.setdefault("schema", SCHEMA_VERSION)
            events.append(event)
    return events


def agent_answer_from_events(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        event_type = str(event.get("type") or "").lower()
        if event_type in {"session_end", "final_answer"}:
            return str(event.get("content") or "").strip()
    return ""


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "agent_event")
    event_type_lower = event_type.lower()
    content = str(event.get("content") or "")
    tool_name = event.get("tool_name")

    if "tool_call" in event_type_lower:
        kind = "tool_call"
    elif "tool_result" in event_type_lower:
        kind = "tool_result"
    elif event_type_lower in {"session_end", "final_answer"}:
        kind = "final_answer"
    elif "error" in event_type_lower:
        kind = "error"
    elif event_type_lower == "done":
        kind = "done"
    else:
        kind = "agent_event"

    summary = content
    if not summary and event.get("metadata"):
        summary = json.dumps(event.get("metadata"), ensure_ascii=False)

    return {
        "kind": kind,
        "schema": event.get("schema") or SCHEMA_VERSION,
        "type": event_type,
        "event_id": event.get("event_id"),
        "parent_event_id": event.get("parent_event_id"),
        "run_id": event.get("run_id"),
        "session_id": event.get("session_id"),
        "tool_name": tool_name,
        "tool_args": event.get("tool_args"),
        "tool_call_id": event.get("tool_call_id"),
        "cited_source_ids": event.get("cited_source_ids") or [],
        "summary": compact(summary, 500),
    }


def normalize_event_stream(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events = payload.get("events") or []
    events = [normalize_event(event) for event in raw_events if isinstance(event, dict)]
    agent_answer = str(payload.get("agent_answer") or "").strip() or agent_answer_from_events(raw_events)
    if agent_answer and not any(event.get("kind") == "final_answer" for event in events):
        events.append(
            {
                "kind": "final_answer",
                "schema": SCHEMA_VERSION,
                "type": "session_end",
                "summary": compact(agent_answer, 500),
            }
        )
    return events


def compact_trace_for_context(trace: dict[str, Any], *, max_events: int = 40) -> dict[str, Any]:
    """Return the PSKA-safe context/capture view of a FastReAct trace.

    Full raw events are useful for UI replay, but they are too noisy and often too
    sensitive for prompts, capture records, or long-term memory.
    """
    if not isinstance(trace, dict):
        return {}
    raw_events = trace.get("events") if isinstance(trace.get("events"), list) else []
    normalized_events = [normalize_event(event) for event in raw_events if isinstance(event, dict)]
    kept_events = [
        event
        for event in normalized_events
        if event.get("kind") in {"tool_call", "tool_result", "final_answer", "error"}
        or str(event.get("type") or "").lower() in {"ask_user", "context_compression"}
    ][:max_events]
    tool_calls = trace.get("tool_calls") if isinstance(trace.get("tool_calls"), list) else []
    compacted: dict[str, Any] = {
        key: trace[key]
        for key in ["run_id", "session_id", "event_count", "model", "usage", "fastreact_metadata", "retrieval_plan", "evidence_check", "status"]
        if key in trace
    }
    compacted["events_kept"] = kept_events
    compacted["raw_event_count"] = len(raw_events)
    compacted["raw_events_retained"] = False
    compacted["tool_calls"] = [
        {
            key: call.get(key)
            for key in ["event_id", "tool_call_id", "tool_name", "tool_args", "status"]
            if isinstance(call, dict) and call.get(key) is not None
        }
        for call in tool_calls[:max_events]
        if isinstance(call, dict)
    ]
    return compacted


def compact(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return "." * max(0, limit)
    return f"{text[: limit - 3]}..."
