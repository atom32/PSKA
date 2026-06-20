from __future__ import annotations

from pathlib import Path

from scripts.twitter_full_report import (
    FIXED_QUESTIONS,
    acceptance_section,
    bottleneck_section,
    build_questions,
    build_parser,
    default_technical_paths,
    derive_acceptance_checks,
    fastreact_event_stream,
    fastreact_event_stream_section,
    parse_json_from_stdout,
    parse_sse_events,
    fastreact_payload_passed,
    provenance_section,
    recovery_section,
    render_html_report,
    render_svg_graph,
    review_section,
    scrub_secrets,
    technical_paths_section,
    write_outputs,
)


def test_html_renderer_escapes_user_content() -> None:
    report = minimal_report()
    report["source_items"] = [{"source_id": "1", "title": "<script>alert(1)</script>", "url": None, "snippet": "safe"}]

    html = render_html_report(report)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_scrubber_removes_secret_and_home_path(monkeypatch) -> None:
    monkeypatch.setenv("PSKA_LLM_API_KEY", "secret-key")
    data = {"nested": ["secret-key", str(Path.home() / "private" / "path")]}

    scrubbed = scrub_secrets(data)

    assert "secret-key" not in str(scrubbed)
    assert "~/private/path" in str(scrubbed)


def test_question_builder_returns_fixed_and_data_derived_questions() -> None:
    questions = build_questions(
        [{"title": "Obscura GitHub browser automation", "snippet": "Claude Code Codex Playwright"}],
        [{"label": "Obscura"}],
    )

    assert any("Obscura" in question for question in questions)
    for fixed in FIXED_QUESTIONS:
        assert fixed in questions


def test_recovery_section_distinguishes_llm_repair_from_forbidden_fallback() -> None:
    html = recovery_section({"recovery_events": [{"kind": "llm_json_repair", "detail": {"reason": "JSONDecodeError"}}]})

    assert "LLM repair events were used" in html
    assert "No forbidden rule-based fallback" in html


def test_graph_renderer_handles_empty_and_multi_member_graph() -> None:
    assert "No graph entities extracted" in render_svg_graph([], [])
    svg = render_svg_graph(
        [
            {"entity_id": "a", "label": "A"},
            {"entity_id": "b", "label": "B"},
            {"entity_id": "c", "label": "C"},
        ],
        [{"members": [{"entity_id": "a"}, {"entity_id": "b"}, {"entity_id": "c"}]}],
    )

    assert "<svg" in svg
    assert svg.count("<line") == 2


def test_parse_json_from_stdout_uses_last_json_line() -> None:
    payload = parse_json_from_stdout("log line\n{\"ok\": true}\n")

    assert payload == {"ok": True}


def test_parse_sse_events_reads_fastreact_agent_events() -> None:
    events = parse_sse_events(
        'event: tool_call\n'
        'data: {"schema":"fastreact.agent_event.v1","tool_name":"pska_search","content":""}\n\n'
        'event: done\n'
        'data: {"type":"final_answer","content":"done"}\n\n'
        "data: [DONE]\n\n"
    )

    assert events == [
        {"schema": "fastreact.agent_event.v1", "tool_name": "pska_search", "content": "", "type": "tool_call"},
        {"type": "final_answer", "content": "done", "schema": "fastreact.agent_event.v1"},
    ]


def test_fastreact_payload_requires_direct_and_full_agent_answers() -> None:
    payload = {
        "direct_search": {"results": [{"snippet": "direct"}]},
        "agent_answer": "",
    }

    assert not fastreact_payload_passed(0, payload)
    payload["agent_answer"] = "full"
    assert fastreact_payload_passed(0, payload)


def test_fastreact_event_stream_normalizes_tools_and_final_answer() -> None:
    payload = {
        "direct_search": {"results": [{"snippet": "direct search result"}]},
        "agent_answer": "",
        "events": [
            {
                "type": "TOOL_CALL",
                "tool_name": "pska_pska_search",
                "tool_args": {"query": "Q"},
                "content": "",
            },
            {
                "type": "SESSION_END",
                "content": "final answer from event stream",
            },
        ],
    }

    events = fastreact_event_stream(payload)
    html = fastreact_event_stream_section(payload)

    assert fastreact_payload_passed(0, payload)
    assert {event["kind"] for event in events} >= {"tool_call", "tool_result", "final_answer"}
    assert "Fastreact Event Stream" in html
    assert "pska_pska_search" in html
    assert "final answer from event stream" in html


def test_report_parser_accepts_stage_selection_flags() -> None:
    args = build_parser().parse_args(
        ["--skip-import", "--only-fastreact", "--run-id", "run_test", "--fastreact-mode", "api"]
    )
    assert args.skip_import is True
    assert args.only_fastreact is True
    assert args.run_id == "run_test"
    assert args.fastreact_mode == "api"


def test_report_renders_technical_paths_acceptance_and_reviews() -> None:
    report = minimal_report()
    report["run_metadata"]["pipeline_steps"] = [
        {"name": "db_reset", "status": "skipped", "reason": "--skip-import", "duration_seconds": 1.0},
        {"name": "http_api_start", "status": "passed", "duration_seconds": 2.5},
    ]
    report["database_summary"]["embedded_chunks"] = 3
    report["recovery_events"] = [{"kind": "llm_json_repair", "detail": {"reason": "bad json"}}]
    report["review_items"] = [
        {
            "review_item_id": "rev_1",
            "review_type": "share_proposal",
            "status": "pending",
            "title": "Share note",
        }
    ]

    html = render_html_report(report)

    assert "Technical Paths" in html
    assert "PSKA direct" in html
    assert "MCP direct" in html
    assert "Fastreact full Agent" in html
    assert "Acceptance Checks" in html
    assert "Duration Bottlenecks" in html
    assert "http_api_start" in html
    assert "Review Items" in html
    assert "rev_1" in html
    assert "pending" in html


def test_write_outputs_adds_json_acceptance_metadata(tmp_path: Path) -> None:
    report = minimal_report()
    report["run_metadata"]["run_id"] = "run/test"
    report["run_metadata"]["history_dir"] = str(tmp_path / "runs")
    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"

    write_outputs(report, html_path, json_path)

    assert html_path.exists()
    assert (tmp_path / "runs" / "run_test" / "report.html").exists()
    assert (tmp_path / "runs" / "run_test" / "report.json").exists()
    written = json_path.read_text(encoding="utf-8")
    assert "technical_paths" in written
    assert "acceptance_checks" in written


def test_acceptance_checks_track_failures_skips_and_recovery_events() -> None:
    report = minimal_report()
    report["run_metadata"]["pipeline_steps"] = [
        {"name": "twitter_zip_import", "status": "failed"},
        {"name": "mcp", "status": "skipped"},
    ]
    report["recovery_events"] = [{"kind": "llm_schema_repair"}]

    checks = derive_acceptance_checks(report)

    assert any(check["name"] == "Failure visibility" and check["status"] == "failed" for check in checks)
    assert any(check["name"] == "Stage selection" and check["status"] == "skipped" for check in checks)
    assert any(check["name"] == "LLM/schema repair visibility" and check["status"] == "passed" for check in checks)


def test_bottleneck_section_orders_steps_by_duration() -> None:
    html = bottleneck_section(
        {
            "pipeline_steps": [
                {"name": "fast", "status": "passed", "duration_seconds": 0.1},
                {"name": "slow", "status": "passed", "duration_seconds": 3.2},
                {"name": "medium", "status": "passed", "duration_seconds": 1.5},
            ]
        }
    )

    assert "Duration Bottlenecks" in html
    assert html.index("slow") < html.index("medium") < html.index("fast")


def test_provenance_section_shows_participant_time_and_source_refs() -> None:
    report = minimal_report()
    report["source_items"] = [
        {
            "source_id": "tweet-1",
            "source_channel": "twitter",
            "record_type": "tweet",
            "author": {"handle": "@alice"},
            "source_created_at": "2026-06-01T00:00:00Z",
            "captured_at": "2026-06-02T00:00:00Z",
            "url": "https://x.com/alice/status/1",
        }
    ]
    report["graph"]["hyperedges"] = [
        {
            "relation_type": "mentions",
            "evidence_text": "Alice mentioned Codex.",
            "source_refs": [{"source_item_id": "src_1", "url": "https://x.com/alice/status/1"}],
        }
    ]

    html = provenance_section(report)

    assert "@alice" in html
    assert "2026-06-01T00:00:00Z" in html
    assert "Alice mentioned Codex." in html
    assert "src_1" in html


def minimal_report() -> dict:
    return {
        "run_metadata": {
            "started_at": "2026-06-10T00:00:00Z",
            "finished_at": "2026-06-10T00:00:01Z",
            "overall_status": "passed",
            "zip_count": 1,
        },
        "pipeline_steps": [{"name": "test", "status": "passed"}],
        "database_summary": {"source_items": 1, "entities": 0, "hyperedges": 0},
        "import_summary": {},
        "extraction_summary": {},
        "graph": {"entities": [], "hyperedges": []},
        "source_items": [],
        "questions": ["Q"],
        "pska_results": [{"question": "Q", "agentic_search": {"answer": "A", "retrieval": {"citations": []}}}],
        "mcp_results": [{"question": "Q"}],
        "fastreact_results": [
            {
                "question": "Q",
                "status": "passed",
                "direct_mcp_status": "passed",
                "full_agent_status": "passed",
                "payload": {"direct_search": {"results": [{"snippet": "D"}]}, "agent_answer": "A"},
            }
        ],
        "recovery_events": [],
        "raw_debug": {},
    }
