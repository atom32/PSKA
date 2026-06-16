from __future__ import annotations

from scripts.current_sample_gate import _has_graph_evidence_citations, _next_actions


def test_has_graph_evidence_citations_checks_context_and_paths() -> None:
    class Retrieval:
        hypergraph_context = []
        graph_paths = [{"edges": [{"evidence_citations": [{"source_item_id": "src_1"}]}]}]

    assert _has_graph_evidence_citations(Retrieval()) is True


def test_next_actions_surface_missing_graph_and_digest_output() -> None:
    actions = _next_actions(
        {
            "sources_exist": True,
            "chunks_exist": True,
            "digest_job_exists": True,
            "entities_exist": False,
            "review_or_memory_exists": False,
            "graph_has_evidence_citations": False,
        }
    )

    assert any("extract-all" in action for action in actions)
    assert any("Fastreact PSKA digest worker" in action for action in actions)
    assert any("graph evidence citations" in action for action in actions)


def test_next_actions_ready_when_requested_checks_pass() -> None:
    assert _next_actions(
        {
            "sources_exist": True,
            "chunks_exist": True,
            "digest_job_exists": True,
            "entities_exist": True,
            "review_or_memory_exists": True,
            "graph_has_evidence_citations": True,
        }
    ) == ["Current sample database passes the requested MVP+ gate."]
