from __future__ import annotations

from typing import Any


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.prompts: list[dict[str, Any]] = []

    def complete_json(self, *, system: str, prompt: str, temperature: float = 0.0) -> dict[str, Any]:
        self.prompts.append({"system": system, "prompt": prompt, "temperature": temperature})
        if not self.responses:
            raise AssertionError("FakeLLM has no remaining responses")
        return self.responses.pop(0)


def extraction_response() -> dict[str, Any]:
    return {
        "entities": [
            {"entity_type": "project", "label": "Project Atlas"},
            {"entity_type": "channel", "label": "Twitter Archive"},
            {"entity_type": "policy", "label": "P-204"},
            {"entity_type": "person_alias", "label": "dependent K"},
            {"entity_type": "stage", "label": "education enrollment"},
            {"entity_type": "agent", "label": "Review Agent"},
        ],
        "hyperedges": [
            {
                "relation_type": "depends_on",
                "directionality": "directed",
                "evidence_text": "Project Atlas depends on the Twitter Archive channel.",
                "confidence": 0.86,
                "members": [
                    {"entity_type": "project", "label": "Project Atlas", "role": "project"},
                    {"entity_type": "channel", "label": "Twitter Archive", "role": "source_channel"},
                ],
            },
            {
                "relation_type": "covers",
                "directionality": "directed",
                "evidence_text": "The policy P-204 covers the education enrollment stage for dependent K.",
                "confidence": 0.91,
                "members": [
                    {"entity_type": "policy", "label": "P-204", "role": "policy"},
                    {"entity_type": "person_alias", "label": "dependent K", "role": "beneficiary"},
                    {"entity_type": "stage", "label": "education enrollment", "role": "stage"},
                ],
            },
            {
                "relation_type": "requires_review",
                "directionality": "directed",
                "evidence_text": "The Review Agent must confirm any team-visible sharing before release.",
                "confidence": 0.83,
                "members": [
                    {"entity_type": "agent", "label": "Review Agent", "role": "reviewer"},
                    {"entity_type": "project", "label": "Project Atlas", "role": "system"},
                ],
            },
        ],
        "review_items": [
            {
                "review_type": "share_proposal",
                "title": "Review team-visible sharing",
                "proposal": {"reason": "The document proposes team-visible sharing."},
            }
        ],
    }


def agentic_plan_response(query: str = "What covers dependent K during education enrollment?") -> dict[str, Any]:
    return {
        "intent": "knowledge_lookup",
        "retrieval_plan": ["acl_filter", "fts", "vector", "rrf", "hypergraph_one_hop", "evidence_check", "answer_synthesis"],
        "retrieval_queries": [query],
        "conflict_check": "check retrieved evidence for contradictions",
        "sensitive_gate": "not_triggered",
    }


def agentic_answer_response(answer: str = "P-204 covers dependent K during education enrollment.") -> dict[str, Any]:
    return {"answer": answer, "confidence": 0.9, "gaps": [], "conflicts": []}
