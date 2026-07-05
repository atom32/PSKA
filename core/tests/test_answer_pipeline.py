from __future__ import annotations

from pska_core.answer_pipeline import (
    AnswerCandidate,
    AnswerPipeline,
    AnswerPipelineContext,
    required_answer_values,
)


def test_answer_pipeline_selects_deterministic_when_agentic_misses_required_values() -> None:
    deterministic = "关键结论：alpha = 10；beta = 20。"
    decision = AnswerPipeline().decide(
        [
            AnswerCandidate(
                answer="alpha 是 10。",
                answer_type="kb_answer",
                owner="fastreact_agentic_service",
                priority=0,
            ),
            AnswerCandidate(
                answer=deterministic,
                answer_type="kb_answer",
                owner="deterministic_fallback",
                priority=10,
            ),
        ],
        AnswerPipelineContext(
            query="alpha 和 beta 分别是多少？",
            ask_intent="kb_search",
            evidence_status="supported",
            required_values=required_answer_values(
                "alpha 和 beta 分别是多少？",
                deterministic,
                ask_intent="kb_search",
            ),
        ),
    )

    assert decision.owner == "deterministic_fallback"
    assert "beta = 20" in decision.answer
    rejected = decision.audit["candidates"][0]
    assert rejected["status"] == "rejected"
    assert rejected["validations"][1]["reason"] == "missing_required_values"


def test_answer_pipeline_records_no_answer_policy_candidate() -> None:
    decision = AnswerPipeline().decide(
        [
            AnswerCandidate(
                answer="关键结论：当前资料不足以回答。",
                answer_type="no_answer",
                owner="no_answer_policy",
                priority=0,
                metadata={"reasons": ["all_citations_dropped"]},
            )
        ],
        AnswerPipelineContext(
            query="能证明一个无关问题吗？",
            ask_intent="kb_search",
            evidence_status="insufficient",
        ),
    )

    assert decision.owner == "no_answer_policy"
    assert decision.answer_type == "no_answer"
    assert decision.audit["selected_owner"] == "no_answer_policy"
