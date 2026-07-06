from __future__ import annotations

from pska_core.api import _ask_apply_evidence_check, _ask_compose_evidence_set
from pska_core.citation_pipeline import CitationSelectionContext, CitationSelectionPipeline


def test_citation_selection_scores_supported_evidence_with_explainable_span() -> None:
    citations = [
        {
            "source_item_id": "source-background",
            "title": "Background memo",
            "snippet": "This memo mentions general context but not the target value.",
            "support_hits": [],
        },
        {
            "source_item_id": "source-answer",
            "title": "Answer memo",
            "snippet": "metric-alpha has value 42 in the selected evidence table.",
            "support_hits": ["metric-alpha", "42"],
            "source_window": {
                "text": "Introductory context.\nmetric-alpha has value 42 in the selected evidence table.",
            },
        },
    ]

    result = CitationSelectionPipeline().select(
        citations,
        CitationSelectionContext(
            query="What is metric-alpha 42?",
            query_terms=("metric-alpha",),
            anchor_terms=("metric-alpha",),
            max_citations=1,
        ),
    )

    assert result.selected[0]["source_item_id"] == "source-answer"
    assert result.selected[0]["citation_selection"]["score"] > 0
    assert "metric-alpha" in result.selected[0]["citation_selection"]["selected_span"]
    assert result.dropped[0]["drop_reason"] == "citation_selection_overflow"
    feature_names = {feature["name"] for feature in result.selected[0]["citation_selection"]["features"]}
    assert {"support_hits", "anchor_coverage", "query_term_coverage"}.issubset(feature_names)


def test_ask_evidence_composition_attaches_evidence_set_after_citation_selection() -> None:
    evidence_check = {
        "status": "supported",
        "query_terms": ["project", "delta", "2024", "2025"],
        "query_anchors": ["project delta"],
        "used_citations": [
            {
                "source_item_id": "source-2024",
                "document_id": "doc-2024",
                "chunk_id": "chunk-2024",
                "title": "Project Delta 2024 memo",
                "snippet": "Project Delta had 40 units in 2024.",
                "source_window": {"text": "Project Delta had 40 units in 2024."},
                "citation_selection": {"rank": 1, "score": 0.4, "selected_span": "Project Delta had 40 units in 2024."},
            },
            {
                "source_item_id": "source-2025",
                "document_id": "doc-2025",
                "chunk_id": "chunk-2025",
                "title": "Project Delta 2025 memo",
                "snippet": "Project Delta had 50 units in 2025.",
                "source_window": {"text": "Project Delta had 50 units in 2025."},
                "citation_selection": {"rank": 2, "score": 0.3, "selected_span": "Project Delta had 50 units in 2025."},
            },
        ],
    }
    composed = _ask_compose_evidence_set(
        query="Compare Project Delta units in 2024 and 2025.",
        evidence_check=evidence_check,
        evidence={"graph_paths": []},
    )
    filtered = _ask_apply_evidence_check({"citations": [], "results": [], "source_windows": []}, composed)

    assert composed["evidence_composition"]["status"] == "composed"
    assert composed["evidence_set"]["schema"] == "pska.evidence_set.v1"
    assert composed["evidence_set"]["missing_slots"] == []
    assert filtered["evidence_set"]["evidence_set_id"] == composed["evidence_set"]["evidence_set_id"]
