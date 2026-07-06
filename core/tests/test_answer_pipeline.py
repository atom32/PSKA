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
    assert any(validation["reason"] == "missing_required_values" for validation in rejected["validations"])


def test_answer_pipeline_selects_deterministic_when_agentic_misses_support_terms() -> None:
    deterministic = "关键结论：Acme Example status is active，owner is Alice Example。"
    decision = AnswerPipeline().decide(
        [
            AnswerCandidate(
                answer="Fake external agentic answer.",
                answer_type="kb_answer",
                owner="fastreact_agentic_service",
                priority=0,
                metadata={"validate_support_term_coverage": True},
            ),
            AnswerCandidate(
                answer=deterministic,
                answer_type="kb_answer",
                owner="deterministic_fallback",
                priority=10,
            ),
        ],
        AnswerPipelineContext(
            query="Acme Example 的状态和负责人是什么？",
            ask_intent="kb_search",
            evidence_status="supported",
            support_terms=("acme", "example", "status", "owner"),
        ),
    )

    assert decision.owner == "deterministic_fallback"
    rejected = decision.audit["candidates"][0]
    assert rejected["status"] == "rejected"
    assert any(validation["reason"] == "missing_support_terms" for validation in rejected["validations"])


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


def test_answer_pipeline_rejects_runtime_control_signal_answers() -> None:
    decision = AnswerPipeline().decide(
        [
            AnswerCandidate(
                answer="[STOPPED] Task stopped due to maximum iteration limit (20).",
                answer_type="deep_answer",
                owner="fastreact_agentic_service",
                priority=0,
            ),
            AnswerCandidate(
                answer="关键结论：当前长链路分析没有形成可审计答案。",
                answer_type="no_answer",
                owner="no_answer_policy",
                priority=10,
            ),
        ],
        AnswerPipelineContext(
            query="请做深入分析",
            ask_intent="kb_search",
            evidence_status="insufficient",
        ),
    )

    assert decision.owner == "no_answer_policy"
    assert decision.audit["candidates"][0]["status"] == "rejected"
    assert any(
        validation["reason"] == "runtime_control_signal"
        for validation in decision.audit["candidates"][0]["validations"]
    )


def test_answer_pipeline_rejects_tool_like_runtime_artifacts() -> None:
    decision = AnswerPipeline().decide(
        [
            AnswerCandidate(
                answer='<tool_call name="search">Searching indexed evidence</tool_call>',
                answer_type="kb_answer",
                owner="fastreact_agentic_service",
                priority=0,
            ),
            AnswerCandidate(
                answer="关键结论：目标指标为 10。",
                answer_type="kb_answer",
                owner="deterministic_fallback",
                priority=10,
            ),
        ],
        AnswerPipelineContext(
            query="目标指标是多少？",
            ask_intent="kb_search",
            evidence_status="supported",
        ),
    )

    assert decision.owner == "deterministic_fallback"
    assert decision.audit["candidates"][0]["status"] == "rejected"
    assert any(
        validation["reason"] == "runtime_artifact"
        for validation in decision.audit["candidates"][0]["validations"]
    )


def test_answer_pipeline_rejects_procedural_runtime_artifacts() -> None:
    decision = AnswerPipeline().decide(
        [
            AnswerCandidate(
                answer="I'll search the indexed evidence and then retrieve more context.",
                answer_type="kb_answer",
                owner="fastreact_agentic_service",
                priority=0,
            ),
            AnswerCandidate(
                answer="关键结论：目标指标为 10。",
                answer_type="kb_answer",
                owner="deterministic_fallback",
                priority=10,
            ),
        ],
        AnswerPipelineContext(
            query="目标指标是多少？",
            ask_intent="kb_search",
            evidence_status="supported",
        ),
    )

    assert decision.owner == "deterministic_fallback"
    assert any(
        validation["reason"] == "runtime_artifact"
        for validation in decision.audit["candidates"][0]["validations"]
    )


def test_answer_pipeline_rejects_task_mismatch_when_evidence_is_supported() -> None:
    decision = AnswerPipeline().decide(
        [
            AnswerCandidate(
                answer="The user request appears truncated; I cannot determine the specific query or task.",
                answer_type="kb_answer",
                owner="fastreact_agentic_service",
                priority=0,
            ),
            AnswerCandidate(
                answer="关键结论：目标指标为 10。",
                answer_type="kb_answer",
                owner="deterministic_fallback",
                priority=10,
            ),
        ],
        AnswerPipelineContext(
            query="目标指标是多少？",
            ask_intent="kb_search",
            evidence_status="supported",
        ),
    )

    assert decision.owner == "deterministic_fallback"
    assert any(
        validation["reason"] == "task_mismatch"
        for validation in decision.audit["candidates"][0]["validations"]
    )


def test_required_answer_values_does_not_treat_plain_comma_as_multi_value_request() -> None:
    deterministic = "关键结论：alpha = 10；beta = 20。"

    assert required_answer_values(
        "alpha 是多少？请给出关键数值，并用一句话回答。",
        deterministic,
        ask_intent="kb_search",
    ) == ()
