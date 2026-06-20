from __future__ import annotations

from scripts.mvp_plus_smoke import build_smoke_report


def test_mvp_plus_smoke_covers_limited_end_to_end_flow() -> None:
    report = build_smoke_report()

    assert report["ok"] is True
    assert all(report["checks"].values())
    assert report["counts"]["source_items"] == 3
    assert report["counts"]["agent_memories"] == 1
    assert report["counts"]["profile_cards"] == 1
    assert report["counts"]["jobs"] == 1
    assert report["sample"]["direct_qa_citations"]
    assert report["sample"]["graph_path"]["explanation"]
    assert report["sample"]["memory_context"][0]["citations"]
    assert report["sample"]["profile_context"][0]["citations"]
    assert report["sample"]["conflicts"]
    assert report["sample"]["sensitivity"]
