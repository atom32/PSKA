from __future__ import annotations

from pathlib import Path

from scripts.twitter_full_report import (
    FIXED_QUESTIONS,
    build_questions,
    parse_json_from_stdout,
    fastreact_payload_passed,
    recovery_section,
    render_html_report,
    render_svg_graph,
    scrub_secrets,
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


def test_fastreact_payload_requires_direct_and_full_agent_answers() -> None:
    payload = {
        "direct_agentic_search": {"answer": "direct"},
        "agent_answer": "",
    }

    assert not fastreact_payload_passed(0, payload)
    payload["agent_answer"] = "full"
    assert fastreact_payload_passed(0, payload)


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
                "payload": {"direct_agentic_search": {"answer": "D"}, "agent_answer": "A"},
            }
        ],
        "recovery_events": [],
        "raw_debug": {},
    }
