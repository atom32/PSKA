from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.mvp_plus_real_sample_smoke import (
    materialize_sample_zips,
    select_sample_zips,
    _query_commands,
    _retrieval_output_checks,
)


def test_select_sample_zips_limits_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "b.zip").write_bytes(b"b")
    (tmp_path / "a.zip").write_bytes(b"a")
    (tmp_path / "not_zip.txt").write_text("ignore", encoding="utf-8")

    selected = select_sample_zips(tmp_path, limit=1)

    assert [path.name for path in selected] == ["a.zip"]


def test_select_sample_zips_rejects_non_positive_limit(tmp_path: Path) -> None:
    (tmp_path / "a.zip").write_bytes(b"a")

    assert select_sample_zips(tmp_path, limit=0) == []


def test_materialize_sample_zips_preserves_zip_names(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    sample_dir = tmp_path / "sample"
    source_dir.mkdir()
    first = source_dir / "a.zip"
    second = source_dir / "b.zip"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    materialized = materialize_sample_zips([first, second], sample_dir)

    assert [path.name for path in materialized] == ["001-a.zip", "002-b.zip"]
    assert all(path.exists() for path in materialized)


def test_query_commands_skip_llm_excludes_agentic_search() -> None:
    args = argparse.Namespace(python="python", owner_user_id="user_primary", skip_llm=True)

    commands = _query_commands(args, "postgresql:///pska_test", "archive")

    assert [label for label, _command in commands] == ["search"]


def test_query_commands_includes_agentic_search_when_llm_enabled() -> None:
    args = argparse.Namespace(python="python", owner_user_id="user_primary", skip_llm=False)

    commands = _query_commands(args, "postgresql:///pska_test", "archive")

    assert [label for label, _command in commands] == ["search", "agentic_search"]


def test_retrieval_output_checks_require_search_and_graph_citations() -> None:
    stdout = json.dumps(
        {
            "citations": [{"source_item_id": "src_1"}],
            "hypergraph_context": [{"evidence_citations": [{"source_item_id": "src_1"}]}],
            "graph_paths": [],
        }
    )

    assert _retrieval_output_checks(stdout, include_graph=True) == {
        "search_has_citations": True,
        "graph_has_evidence_citations": True,
    }


def test_retrieval_output_checks_skip_graph_when_llm_is_disabled() -> None:
    stdout = json.dumps({"citations": [{"source_item_id": "src_1"}], "hypergraph_context": []})

    assert _retrieval_output_checks(stdout, include_graph=False) == {"search_has_citations": True}


def test_retrieval_output_checks_report_invalid_json() -> None:
    assert _retrieval_output_checks("not json", include_graph=True) == {
        "search_has_citations": False,
        "graph_has_evidence_citations": False,
    }
