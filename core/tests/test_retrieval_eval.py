from __future__ import annotations

import copy
import json
from pathlib import Path

from pska_core.acl import ACLService
from pska_core.retrieval import RetrievalService
from pska_core.retrieval_eval import (
    DEFAULT_RETRIEVAL_EVAL_FIXTURE,
    FixtureEmbeddingProvider,
    build_eval_store,
    evaluate_response,
    run_retrieval_eval,
)


def test_retrieval_eval_fixture_reports_expected_hits() -> None:
    report = run_retrieval_eval(DEFAULT_RETRIEVAL_EVAL_FIXTURE)

    assert report["ok"] is True
    by_id = {case["case_id"]: case for case in report["cases"]}
    assert by_id["lexical_fastreact"]["lexical"]["missing"] == []
    assert by_id["vector_codegraph"]["vector"]["missing"] == []
    zh_cases = [case for case in report["cases"] if str(case["case_id"]).startswith("zh_")]
    assert len(zh_cases) >= 5
    assert by_id["zh_workspace_components"]["citations"]["missing"] == []
    assert by_id["zh_writer_evidence_graph_path"]["graph_paths"]["missing"] == []
    assert by_id["zh_memory_preference"]["memory"]["missing"] == []
    assert by_id["zh_profile_writing_preference"]["profile"]["missing"] == []
    graph = by_id["graph_two_hop_digest"]["graph_paths"]
    assert graph["missing"] == []
    assert any(
        detail["explanation"] == "PSKA -[delegates_to]-> FastReAct -[executes]-> Digest"
        and detail["score_debug"]["evidence_coverage"] == 1.0
        for detail in graph["details"]
    )
    assert by_id["graph_conflict"]["conflicts"]["missing"] == []
    assert report["embedding"]["provider"] == "fixture-embeddings"
    assert report["llm"]["required"] is False


def test_retrieval_eval_failure_report_includes_missing_refs_and_diagnostics() -> None:
    fixture = json.loads(Path(DEFAULT_RETRIEVAL_EVAL_FIXTURE).read_text(encoding="utf-8"))
    case = copy.deepcopy(fixture["cases"][0])
    case["expected_citations"] = ["src_missing#chk_missing"]
    store = build_eval_store(fixture)
    response = RetrievalService(
        store,
        ACLService(store),
        embedding_provider=FixtureEmbeddingProvider(fixture.get("query_vectors") or {}),
    ).search(case["query"], store.get_user("user_primary"), top_k=case["top_k"])

    report = evaluate_response(case, response)

    assert report["ok"] is False
    assert report["query"] == "FastReAct delegates agentic work"
    assert report["citations"]["missing"] == ["src_missing#chk_missing"]
    assert report["diagnostics"]["score_debug"]["lexical_candidates"] >= 1
    assert report["diagnostics"]["graph_path_explanations"]
