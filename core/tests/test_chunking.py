from __future__ import annotations

from pska_core.chunking import chunk_text, preview_chunking


def test_chunking_preview_keeps_offsets_and_heading_context() -> None:
    text = "# Intro\n\nAlpha beta gamma.\n\n## Details\n\nDelta epsilon zeta."

    preview = preview_chunking(text, {"strategy": "auto", "chunk_size": 30, "chunk_overlap": 0})

    assert preview["strategy"] == "heading"
    assert preview["stats"]["count"] >= 2
    first = preview["chunks"][0]
    assert text[first["start"] : first["end"]] == first["text"]
    assert any(chunk["context_header"] == "Details" for chunk in preview["chunks"])


def test_chunking_fixed_overlap_matches_legacy_small_text() -> None:
    spans = chunk_text("abcdefghijkl", {"strategy": "fixed", "chunk_size": 6, "chunk_overlap": 2})

    assert [span.text for span in spans] == ["abcdef", "efghij", "ijkl"]
    assert [(span.start, span.end) for span in spans] == [(0, 6), (4, 10), (8, 12)]


def test_chunking_preview_profiles_tables_code_and_cjk() -> None:
    text = "标题\n\n| A | B |\n| - | - |\n| 甲 | 乙 |\n\n```python\nprint('x')\n```"

    preview = preview_chunking(text, {"strategy": "recursive", "chunk_size": 120})

    assert preview["profile"]["has_markdown_table"] is True
    assert preview["profile"]["has_code_fence"] is True
    assert preview["profile"]["cjk_chars"] >= 3


def test_chunking_preview_heuristic_and_parent_windows() -> None:
    text = "\n\n".join(
        [
            "| A | B |\n| - | - |\n| shared | topic |",
            "```python\nprint('chunking')\n```",
            "这是一段较长中文文本，用来验证 heuristic adaptive chunking 会保留结构块并返回父窗口。",
        ]
    )

    preview = preview_chunking(text, {"strategy": "adaptive", "chunk_size": 60, "parent_chunk_size": 180})

    assert preview["strategy"] == "heuristic"
    assert preview["parent_windows"]
    assert all(chunk["parent_window_ordinal"] is not None for chunk in preview["chunks"])
    assert preview["strategy_diagnostics"]["reason"] == "tables_code_or_long_cjk_detected"
