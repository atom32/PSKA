from __future__ import annotations

from pska_core.evidence_composition import (
    EvidenceCompositionContext,
    EvidenceCompositionPipeline,
    evidence_set_to_dict,
)


def test_evidence_composition_covers_requested_year_slots_from_citations() -> None:
    citations = [
        _citation(
            source_item_id="source-2024",
            title="Project Alpha 2024 metrics",
            text="2024 revenue was 100 and margin was 20%.",
            rank=1,
        ),
        _citation(
            source_item_id="source-2025",
            title="Project Alpha 2025 metrics",
            text="2025 revenue was 125 and margin was 25%.",
            rank=2,
        ),
    ]

    result = EvidenceCompositionPipeline().compose(
        citations,
        EvidenceCompositionContext(
            query="Compare Project Alpha revenue in 2024 and 2025.",
            query_terms=("project", "alpha", "revenue", "2024", "2025"),
            anchor_terms=("project alpha", "revenue"),
        ),
    )
    evidence_set = evidence_set_to_dict(result.evidence_set)

    assert evidence_set["status"] == "composed"
    assert evidence_set["missing_slots"] == []
    slots = {slot["name"]: slot for slot in evidence_set["slots"]}
    assert slots["year:2024"]["covered"] is True
    assert slots["year:2025"]["covered"] is True
    assert len(slots["year:2024"]["record_ids"]) == 1
    assert len(slots["year:2025"]["record_ids"]) == 1
    assert result.audit["source_type_counts"] == {"document": 2}


def test_evidence_composition_marks_missing_required_slots_without_answering() -> None:
    citations = [
        _citation(
            source_item_id="source-2025",
            title="Project Beta 2025 metrics",
            text="2025 active users were 900.",
            rank=1,
        )
    ]

    result = EvidenceCompositionPipeline().compose(
        citations,
        EvidenceCompositionContext(query="Compare Project Beta active users in 2024 and 2025."),
    )

    assert result.evidence_set.status == "incomplete"
    assert "year:2024" in result.evidence_set.missing_slots
    assert "year:2025" not in result.evidence_set.missing_slots
    validation = next(item for item in result.audit["validations"] if item["name"] == "required_slot_coverage")
    assert validation["passed"] is False
    assert validation["reason"] == "missing_required_slots"


def test_evidence_composition_accepts_graph_as_evidence_source_member() -> None:
    result = EvidenceCompositionPipeline().compose(
        [
            _citation(
                source_item_id="source-doc",
                title="System note",
                text="System Gamma is connected to Workflow Delta.",
                rank=1,
            )
        ],
        EvidenceCompositionContext(query="How is System Gamma connected to Workflow Delta?"),
        graph_paths=[
            {
                "explanation": "System Gamma -[triggers]-> Workflow Delta",
                "entities": ["System Gamma", "Workflow Delta"],
                "edge_count": 1,
                "grounded_edges": 1,
                "score": 0.84,
            }
        ],
    )

    evidence_set = evidence_set_to_dict(result.evidence_set)

    assert evidence_set["status"] == "composed"
    assert result.audit["source_type_counts"] == {"document": 1, "graph": 1}
    assert [record["source_type"] for record in evidence_set["records"]] == ["document", "graph"]


def _citation(*, source_item_id: str, title: str, text: str, rank: int) -> dict[str, object]:
    return {
        "source_item_id": source_item_id,
        "document_id": f"doc-{source_item_id}",
        "chunk_id": f"chunk-{source_item_id}",
        "title": title,
        "snippet": text,
        "source_window": {"text": text},
        "citation_selection": {
            "rank": rank,
            "score": 0.5 / rank,
            "selected_span": text,
            "features": [{"name": "support_hits", "value": 1.0, "weight": 0.16, "contribution": 0.16}],
        },
    }
