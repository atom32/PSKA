from __future__ import annotations

import json
from typing import Any

from pska_core.acl import ACLService
from pska_core.candidates import CandidateWriteService
from pska_core.enums import UserRole
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import SourceRef, User
from pska_core.retrieval import RetrievalService
from pska_core.serde import dumps, to_jsonable
from pska_core.store import InMemoryKnowledgeStore


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.prompts: list[dict[str, Any]] = []

    def complete_json(self, *, system: str, prompt: str, temperature: float = 0.0) -> dict[str, Any]:
        self.prompts.append({"system": system, "prompt": prompt, "temperature": temperature})
        if not self.responses:
            raise AssertionError("FakeLLM has no remaining responses")
        return self.responses.pop(0)


def build_smoke_report() -> dict[str, Any]:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))

    ingest = IngestService(store)
    planning_source = ingest.ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "planning_note",
            "source_id": "mvp-plus-planning",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "MVP+ Planning Note",
            "content": {
                "text": (
                    "Project Atlas is the shared knowledge-base initiative. "
                    "The policy P-204 covers the education enrollment stage for dependent K. "
                    "The Review Agent must confirm team-visible sharing."
                )
            },
            "created_at": "2026-06-01T00:00:00Z",
        }
    )
    digest_source = ingest.ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "digest_note",
            "source_id": "mvp-plus-digest",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "MVP+ Digest Note",
            "content": {
                "text": (
                    "PSKA delegates complex agentic work to FastReAct. "
                    "FastReAct executes digest loops for PSKA. "
                    "The user prefers concise PSKA answers."
                )
            },
            "created_at": "2026-06-10T00:00:00Z",
        }
    )
    conflict_source = ingest.ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "claim_note",
            "source_id": "mvp-plus-conflict",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "MVP+ Conflict Note",
            "content": {"text": "Claim A contradicts Claim B."},
            "created_at": "2026-06-11T00:00:00Z",
        }
    )

    llm = FakeLLM([_extraction_response()])
    extraction_report = ExtractionService(store, llm=llm).extract_source_item(planning_source)

    candidate_summary = CandidateWriteService(store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "producer": "mvp_plus_smoke",
            "request_id": "mvp-plus-smoke",
            "source_refs": [{"source_item_id": digest_source.source_item_id}],
            "entities": [
                {"entity_type": "project", "label": "PSKA"},
                {"entity_type": "service", "label": "FastReAct", "metadata": {"aliases": ["FR", "FastReact"]}},
                {"entity_type": "workflow", "label": "Digest"},
            ],
            "hyperedges": [
                {
                    "relation_type": "delegates_to",
                    "directionality": "directed",
                    "evidence_text": "PSKA delegates complex agentic work to FastReAct.",
                    "confidence": 0.9,
                    "members": [
                        {"entity_type": "project", "label": "PSKA", "role": "caller"},
                        {"entity_type": "service", "label": "FastReAct", "role": "executor"},
                    ],
                },
                {
                    "relation_type": "executes",
                    "directionality": "directed",
                    "evidence_text": "FastReAct executes digest loops for PSKA.",
                    "confidence": 0.86,
                    "members": [
                        {"entity_type": "service", "label": "FastReAct", "role": "executor"},
                        {"entity_type": "workflow", "label": "Digest", "role": "workflow"},
                    ],
                },
            ],
            "memory_candidates": [
                {
                    "kind": "agent_memory",
                    "layer": "semantic",
                    "text": "User prefers concise PSKA answers.",
                    "confidence": 0.9,
                },
                {
                    "kind": "profile",
                    "profile_delta": {"communication": {"style": "concise"}},
                    "confidence": 0.8,
                },
            ],
        }
    )
    conflict_summary = CandidateWriteService(store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "producer": "mvp_plus_smoke",
            "request_id": "mvp-plus-conflict",
            "source_refs": [{"source_item_id": conflict_source.source_item_id}],
            "entities": [
                {"entity_type": "claim", "label": "Claim A"},
                {"entity_type": "claim", "label": "Claim B"},
            ],
            "hyperedges": [
                {
                    "relation_type": "contradicts",
                    "evidence_text": "Claim A contradicts Claim B.",
                    "confidence": 0.9,
                    "members": [
                        {"entity_type": "claim", "label": "Claim A", "role": "left"},
                        {"entity_type": "claim", "label": "Claim B", "role": "right"},
                    ],
                }
            ],
        }
    )

    digest_job = JobService(store).submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": digest_source.source_item_id}],
            "scope": {"source_item_ids": [digest_source.source_item_id]},
        },
    )

    retrieval = RetrievalService(store, ACLService(store))
    qa = retrieval.search("What covers dependent K during education enrollment?", store.get_user("user_primary"))
    graph = retrieval.search("PSKA Digest FastReAct relation path", store.get_user("user_primary"))
    memory = retrieval.search("concise PSKA preference", store.get_user("user_primary"))
    profile = retrieval.search("profile communication style", store.get_user("user_primary"))
    conflict = retrieval.search("Claim A Claim B", store.get_user("user_primary"))
    sensitive = retrieval.search("api key rotation", store.get_user("user_primary"))
    mcp_search = _mcp_search(store, "PSKA Digest FastReAct")

    checks = {
        "limited_sources_ingested": len(store.list_source_items()) == 3,
        "agentic_extraction_created_graph": bool(extraction_report.entities_created and extraction_report.hyperedges_created),
        "candidate_write_created_memory_profile_graph": bool(
            candidate_summary["hyperedges"]
            and candidate_summary["agent_memories"]
            and candidate_summary["profile_cards"]
        ),
        "job_backlog_created": digest_job.status == "queued",
        "direct_qa_has_citations": bool(qa.citations),
        "graphrag_has_grounded_path": bool(graph.graph_paths and graph.graph_paths[0]["edges"][0]["evidence_citations"]),
        "memory_context_has_citation": bool(memory.memory_context and memory.memory_context[0]["citations"]),
        "profile_context_has_citation": bool(profile.profile_context and profile.profile_context[0]["citations"]),
        "conflict_diagnostic_present": bool(conflict.conflicts),
        "sensitivity_flag_present": bool(sensitive.sensitivity),
        "mcp_search_returns_content": bool(mcp_search.get("result", {}).get("content")),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "counts": {
            "source_items": store.count_table("source_items"),
            "chunks": store.count_table("chunks"),
            "entities": store.count_table("entities"),
            "hyperedges": store.count_table("hyperedges"),
            "review_items": store.count_table("review_items"),
            "agent_memories": store.count_table("agent_memories"),
            "profile_cards": store.count_table("user_profile_cards"),
            "jobs": store.count_table("jobs"),
        },
        "sample": {
            "source_item_ids": [item.source_item_id for item in store.list_source_items()],
            "extraction_report": to_jsonable(extraction_report),
            "candidate_summary": candidate_summary,
            "conflict_summary": conflict_summary,
            "digest_job": to_jsonable(digest_job),
            "direct_qa_citations": to_jsonable(qa.citations),
            "graph_path": to_jsonable(graph.graph_paths[0] if graph.graph_paths else None),
            "memory_context": to_jsonable(memory.memory_context),
            "profile_context": to_jsonable(profile.profile_context),
            "conflicts": conflict.conflicts,
            "sensitivity": sensitive.sensitivity,
        },
    }


def main() -> int:
    report = build_smoke_report()
    print(dumps(report))
    return 0 if report["ok"] else 1


def _mcp_search(store: InMemoryKnowledgeStore, query: str) -> dict[str, Any]:
    server = MCPServer("postgresql:///unused", store=store)
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": "mvp-plus-search",
            "method": "tools/call",
            "params": {
                "name": "pska_search",
                "arguments": {"query": query, "user_id": "user_primary"},
            },
        }
    )


def _extraction_response() -> dict[str, Any]:
    return {
        "entities": [
            {"entity_type": "project", "label": "Project Atlas"},
            {"entity_type": "policy", "label": "P-204"},
            {"entity_type": "person_alias", "label": "dependent K"},
            {"entity_type": "stage", "label": "education enrollment"},
            {"entity_type": "agent", "label": "Review Agent"},
        ],
        "hyperedges": [
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
                "evidence_text": "The Review Agent must confirm team-visible sharing.",
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


def _agentic_plan_response() -> dict[str, Any]:
    return {
        "intent": "knowledge_lookup",
        "retrieval_plan": ["acl_filter", "fts", "vector", "rrf", "hypergraph_one_hop", "evidence_check", "answer_synthesis"],
        "retrieval_queries": ["What covers dependent K during education enrollment?"],
        "conflict_check": "check retrieved graph conflicts",
        "sensitive_gate": "use retrieval sensitivity flags",
    }


def _agentic_answer_response() -> dict[str, Any]:
    return {
        "answer": "Policy P-204 covers dependent K during education enrollment.",
        "confidence": 0.9,
        "gaps": [],
        "conflicts": [],
    }


if __name__ == "__main__":
    raise SystemExit(main())
