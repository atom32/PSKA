from __future__ import annotations

from dataclasses import asdict, dataclass, field

from pska_core.llm import LLMClient, LLMResponseError, OpenAILLMClient, record_recovery_event
from pska_core.models import User
from pska_core.retrieval import RetrievalResponse, RetrievalService


@dataclass(slots=True)
class AgenticSearchTrace:
    query_understanding: dict[str, str]
    retrieval_plan: list[str]
    iterations: list[dict[str, str]] = field(default_factory=list)
    evidence_check: str = "not_run"
    conflict_check: str = "not_run"
    sensitive_gate: str = "not_triggered"


@dataclass(slots=True)
class AgenticSearchResponse:
    retrieval: RetrievalResponse
    trace: AgenticSearchTrace
    answer: str = ""

    def to_dict(self) -> dict:
        return {
            "retrieval": asdict(self.retrieval),
            "trace": asdict(self.trace),
            "answer": self.answer,
        }


class AgenticSearchService:
    """LLM-required agentic search planner and answer synthesizer."""

    def __init__(self, retrieval: RetrievalService, llm: LLMClient | None = None) -> None:
        self.retrieval = retrieval
        self.llm = llm

    def search(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
    ) -> AgenticSearchResponse:
        plan_spec = self._plan_with_llm(query, max_iterations=max_iterations)
        plan = list(plan_spec["retrieval_plan"])
        iterations: list[dict[str, str]] = []
        retrieval_response = None
        retrieval_queries = list(plan_spec["retrieval_queries"])[: max(1, min(max_iterations, 4))]
        for index, current_query in enumerate(retrieval_queries):
            retrieval_response = self.retrieval.search(
                current_query,
                user,
                represented_user_id=represented_user_id,
            )
            iterations.append({"iteration": str(index + 1), "query": current_query})
            if retrieval_response.results:
                break
        assert retrieval_response is not None
        trace = AgenticSearchTrace(
            query_understanding={
                "intent": str(plan_spec["intent"]),
                "privacy_boundary": "acl_first",
            },
            retrieval_plan=plan,
            iterations=iterations,
            evidence_check="has_citations" if retrieval_response.citations else "insufficient_evidence",
            conflict_check=str(plan_spec.get("conflict_check") or "required_but_not_available"),
            sensitive_gate=str(plan_spec.get("sensitive_gate") or "not_triggered"),
        )
        return AgenticSearchResponse(
            retrieval=retrieval_response,
            trace=trace,
            answer=self._answer_with_llm(query, retrieval_response, trace),
        )

    def _plan_with_llm(self, query: str, *, max_iterations: int) -> dict:
        system = (
            "You are PSKA's agentic retrieval planner. Return strict JSON. "
            "Respect private-first ACL: retrieval execution will apply ACL before any ranking or graph expansion."
        )
        prompt = f"""
Build a PSKA retrieval plan for this user query:
{query}

Return a JSON object with:
- intent: short string
- retrieval_plan: ordered array using available steps only
- retrieval_queries: 1 to {max(1, min(max_iterations, 4))} concrete search strings
- conflict_check: short string
- sensitive_gate: short string

Available steps:
acl_filter, fts, vector, rrf, hypergraph_one_hop, conversation_memory, profile_card, file_lookup, evidence_check, answer_synthesis

Do not answer the question. Plan only.
"""
        llm = self.llm or OpenAILLMClient.from_env()
        raw = llm.complete_json(system=system, prompt=prompt, temperature=0.0)
        try:
            return self._validate_plan(raw)
        except LLMResponseError as exc:
            record_recovery_event("llm_agentic_plan_schema_repair", {"query": query, "error": str(exc)})
            repaired = self._repair_plan_schema(llm, raw, str(exc), query, max_iterations)
            return self._validate_plan(repaired)

    def _validate_plan(self, raw: dict) -> dict:
        try:
            retrieval_queries = [str(item) for item in raw["retrieval_queries"] if str(item).strip()]
            retrieval_plan = [str(item) for item in raw["retrieval_plan"] if str(item).strip()]
            intent = str(raw["intent"])
        except Exception as exc:  # noqa: BLE001
            raise LLMResponseError("Agentic plan JSON must contain intent, retrieval_plan, and retrieval_queries") from exc
        if not retrieval_queries:
            raise LLMResponseError("Agentic plan must include at least one retrieval query")
        if not retrieval_plan or retrieval_plan[0] != "acl_filter":
            raise LLMResponseError("Agentic plan must begin with acl_filter")
        return {
            "intent": intent,
            "retrieval_plan": retrieval_plan,
            "retrieval_queries": retrieval_queries,
            "conflict_check": str(raw.get("conflict_check") or ""),
            "sensitive_gate": str(raw.get("sensitive_gate") or ""),
        }

    def _repair_plan_schema(self, llm: LLMClient, raw: dict, error: str, query: str, max_iterations: int) -> dict:
        system = (
            "You are PSKA's agentic plan schema correction agent. Return strict JSON only. "
            "Do not answer the user question. Do not add facts."
        )
        prompt = f"""
The previous agentic retrieval plan failed schema validation:
{error}

Original user query:
{query}

Convert this object to exactly:
{{
  "intent": "string",
  "retrieval_plan": ["acl_filter", "fts", "vector", "rrf", "hypergraph_one_hop", "evidence_check", "answer_synthesis"],
  "retrieval_queries": ["1 to {max(1, min(max_iterations, 4))} concrete search strings"],
  "conflict_check": "string",
  "sensitive_gate": "string"
}}

Previous plan:
{raw}
"""
        return llm.complete_json(system=system, prompt=prompt, temperature=0.0)

    def _answer_with_llm(self, query: str, retrieval: RetrievalResponse, trace: AgenticSearchTrace) -> str:
        system = (
            "You are PSKA's answer synthesis agent. Return strict JSON. "
            "Answer only from provided citations and hypergraph context. "
            "If evidence is insufficient, say so directly. Do not invent facts."
        )
        prompt = f"""
Question:
{query}

Retrieval JSON:
{asdict(retrieval)}

Agentic trace:
{asdict(trace)}

Return a JSON object with:
- answer: concise answer string
- confidence: number from 0 to 1
- gaps: array of strings
- conflicts: array of strings
"""
        llm = self.llm or OpenAILLMClient.from_env()
        raw = llm.complete_json(system=system, prompt=prompt, temperature=0.0)
        answer = raw.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise LLMResponseError("Answer synthesis JSON must contain a non-empty answer")
        return answer
