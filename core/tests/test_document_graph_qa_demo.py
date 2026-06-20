from __future__ import annotations

from scripts.document_graph_qa_demo import build_demo
from tests.fakes import FakeLLM, extraction_response


def test_document_to_knowledge_graph_to_grounded_retrieval_demo() -> None:
    demo = build_demo(
        llm=FakeLLM([extraction_response()])
    )

    assert demo["source_item_id"].startswith("src_")
    assert {entity["label"] for entity in demo["entities"]} >= {
        "P-204",
        "dependent K",
        "education enrollment",
    }
    assert demo["citations"]
    assert demo["retrieval_results"]
    assert any(edge["relation_type"] == "covers" for edge in demo["hypergraph_context"])
