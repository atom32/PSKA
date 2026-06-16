from __future__ import annotations

from scripts.mvp_plus_http_smoke import build_http_smoke_report


def test_mvp_plus_http_smoke_covers_online_service_flow() -> None:
    report = build_http_smoke_report()

    assert report["ok"] is True
    assert all(report["checks"].values())
    assert report["sample"]["agentic_answer"] == "Policy P-204 covers dependent K during education enrollment."
    assert report["sample"]["graph_path"]["edges"][0]["evidence_citations"]
    assert report["sample"]["memory_context"][0]["citations"]
    assert report["sample"]["profile_context"][0]["citations"]
    assert report["sample"]["conflicts"]
    assert report["sample"]["sensitivity"]
