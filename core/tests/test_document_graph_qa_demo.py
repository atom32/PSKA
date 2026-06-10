from __future__ import annotations

from scripts.document_graph_qa_demo import build_demo
from tests.fakes import FakeLLM, agentic_answer_response, agentic_plan_response, extraction_response


def test_document_to_knowledge_graph_to_agentic_qa_demo() -> None:
    demo = build_demo(
        llm=FakeLLM([
            extraction_response(),
            agentic_plan_response(),
            agentic_answer_response("P-204 covers dependent K during education enrollment."),
        ])
    )

    assert demo["source_item_id"].startswith("src_")
    assert {entity["label"] for entity in demo["entities"]} >= {
        "P-204",
        "dependent K",
        "education enrollment",
    }
    assert "P-204 covers dependent K" in demo["answer"]
    assert demo["citations"]
    assert any(edge["relation_type"] == "covers" for edge in demo["hypergraph_context"])
    assert demo["agentic_trace"]["evidence_check"] == "has_citations"
