from pska_core.fastreact_protocol import (
    SCHEMA_VERSION,
    agent_answer_from_events,
    normalize_event,
    normalize_event_stream,
    parse_sse_events,
)


def test_parse_sse_events_reads_fastreact_schema_frames() -> None:
    text = "\n\n".join(
        [
            'event: tool_call\ndata: {"schema":"fastreact.agent_event.v1","type":"tool_call","tool_name":"pska_pska_search"}',
            'event: session_end\ndata: {"schema":"fastreact.agent_event.v1","type":"session_end","content":"final"}',
            "event: done\ndata: [DONE]",
        ]
    )

    events = parse_sse_events(text)

    assert [event["type"] for event in events] == ["tool_call", "session_end", "done"]
    assert events[0]["schema"] == SCHEMA_VERSION
    assert events[0]["tool_name"] == "pska_pska_search"
    assert agent_answer_from_events(events) == "final"


def test_parse_sse_events_marks_invalid_json() -> None:
    events = parse_sse_events("event: message\ndata: not-json\n\n")

    assert events == [
        {
            "schema": SCHEMA_VERSION,
            "type": "invalid_sse_json",
            "content": "not-json",
        }
    ]


def test_normalize_event_classifies_tool_and_answer_events() -> None:
    tool = normalize_event(
        {
            "schema": SCHEMA_VERSION,
            "type": "tool_call",
            "event_id": "run:1",
            "run_id": "run",
            "session_id": "session",
            "tool_name": "pska_pska_search",
            "tool_args": {"query": "Atlas"},
            "tool_call_id": "call-1",
        }
    )
    answer = normalize_event({"type": "session_end", "content": "final"})

    assert tool["kind"] == "tool_call"
    assert tool["tool_name"] == "pska_pska_search"
    assert tool["tool_args"] == {"query": "Atlas"}
    assert tool["tool_call_id"] == "call-1"
    assert answer["kind"] == "final_answer"
    assert answer["summary"] == "final"


def test_normalize_event_stream_adds_final_answer_fallback() -> None:
    stream = normalize_event_stream(
        {
            "agent_answer": "final answer",
            "events": [
                {
                    "type": "tool_result",
                    "tool_name": "pska_pska_search",
                    "content": "evidence",
                    "cited_source_ids": ["source-1"],
                }
            ],
        }
    )

    assert [event["kind"] for event in stream] == ["tool_result", "final_answer"]
    assert stream[0]["cited_source_ids"] == ["source-1"]
    assert stream[-1]["summary"] == "final answer"
