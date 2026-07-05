from __future__ import annotations

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
