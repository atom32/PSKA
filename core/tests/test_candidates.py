from __future__ import annotations

import json

from pska_core.api import PSKAApi
from pska_core.candidates import CandidateWriteError, CandidateWriteService
from pska_core.enums import ReviewType, UserRole, Visibility
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, EXTRACT_VIA_FASTREACT, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import User
from pska_core.retrieval import RetrievalService
from pska_core.acl import ACLService
from pska_core.review import ReviewService
from pska_core.store import InMemoryKnowledgeStore


def test_write_candidates_creates_grounded_knowledge_objects() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))

    summary = CandidateWriteService(store).write_candidates(
        {
            "owner_user_id": "user_primary",
            "job_id": "job_candidates",
            "request_id": "run_candidates",
            "source_refs": [{"source_item_id": source_id}],
            "entities": [{"entity_type": "project", "label": "PSKA", "confidence": 0.9}],
            "hyperedges": [
                {
                    "relation_type": "depends_on",
                    "evidence_text": "PSKA depends on Fastreact for agentic loops.",
                    "confidence": 0.85,
                    "members": [
                        {"entity_type": "project", "label": "PSKA", "role": "system"},
                        {"entity_type": "service", "label": "Fastreact", "role": "dependency"},
                    ],
                }
            ],
            "review_items": [
                {
                    "review_type": "conflict",
                    "title": "Check agentic boundary wording",
                    "proposal": {"note": "Review boundary statement"},
                }
            ],
            "memory_candidates": [
                {"kind": "agent_memory", "layer": "semantic", "text": "PSKA keeps Postgres as the source of truth.", "confidence": 0.8}
            ],
        }
    )

    assert len(summary["entities"]) == 2
    assert len(summary["hyperedges"]) == 1
    assert len(summary["review_items"]) == 1
    assert len(summary["agent_memories"]) == 1
    assert summary["schema_version"] == "pska.candidates.v1"
    assert summary["warnings"] == ["schema_version missing; assumed pska.candidates.v1"]
    assert next(iter(store.hyperedges.values())).source_refs[0].source_item_id == source_id
    assert store.list_review_items()[0].proposal["source_refs"][0]["source_item_id"] == source_id
    assert store.list_audit_events("candidate_batch", "run_candidates")[0].decision == "accepted"


def test_write_candidates_requires_known_source_refs() -> None:
    store = _store()

    try:
        CandidateWriteService(store).write_candidates(
            {
                "owner_user_id": "user_primary",
                "source_refs": [{"source_item_id": "missing"}],
                "entities": [{"entity_type": "project", "label": "PSKA"}],
            }
        )
    except CandidateWriteError as exc:
        assert "known source_items" in str(exc)
    else:
        raise AssertionError("expected CandidateWriteError")


def test_low_confidence_memory_candidate_requires_review() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))

    summary = CandidateWriteService(store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source_id}],
            "memory_candidates": [
                {"kind": "agent_memory", "layer": "semantic", "text": "Maybe PSKA prefers very long answers.", "confidence": 0.4}
            ],
        }
    )

    review = store.get_review_item(summary["review_items"][0])
    assert summary["agent_memories"] == []
    assert review.review_type == ReviewType.LOW_CONFIDENCE
    assert review.proposal["memory_candidate"] == "Maybe PSKA prefers very long answers."
    assert review.proposal["confidence"] == 0.4


def test_low_confidence_relationship_candidate_requires_review() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))

    summary = CandidateWriteService(store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source_id}],
            "hyperedges": [
                {
                    "relation_type": "depends_on",
                    "confidence": 0.3,
                    "members": [
                        {"entity_type": "project", "label": "PSKA", "role": "system"},
                        {"entity_type": "service", "label": "Fastreact", "role": "dependency"},
                    ],
                }
            ],
        }
    )

    review = store.get_review_item(summary["review_items"][0])
    assert summary["hyperedges"] == []
    assert review.review_type == ReviewType.RELATIONSHIP_CANDIDATE
    assert review.proposal["relation_type"] == "depends_on"
    assert review.proposal["reason"] == "low_confidence_relationship_candidate"


def test_write_candidates_rejects_unknown_schema_version() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))

    try:
        CandidateWriteService(store).write_candidates(
            {
                "schema_version": "pska.candidates.v999",
                "owner_user_id": "user_primary",
                "source_refs": [{"source_item_id": source_id}],
            }
        )
    except CandidateWriteError as exc:
        assert "unsupported candidate schema_version" in str(exc)
    else:
        raise AssertionError("expected CandidateWriteError")


def test_mcp_write_candidates_tool_returns_summary() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))
    server = MCPServer("postgresql:///unused", store=store)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "pska_write_candidates",
                "arguments": {
                    "owner_user_id": "user_primary",
                    "source_refs": [{"source_item_id": source_id}],
                    "entities": [{"entity_type": "project", "label": "PSKA"}],
                },
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["summary"]["entities"]
    assert len(store.entities) == 1


def test_mcp_job_context_returns_scoped_source_context() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))
    job = JobService(store).submit("digest_via_fastreact", {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": source_id}]})
    server = MCPServer("postgresql:///unused", store=store)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "pska_job_context",
                "arguments": {"job_id": job.job_id, "user_id": "user_primary"},
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["job"]["job_id"] == job.job_id
    assert payload["source_items"][0]["source_item_id"] == source_id
    assert payload["request_user_id"] == "user_primary"


def test_api_write_candidates_route_uses_same_service() -> None:
    api = _api()
    source_id = _ingest_source(api.store)

    response = api.write_candidates(
        {
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source_id}],
            "memory_candidates": [{"kind": "profile", "profile_delta": {"prefers": "citations"}, "confidence": 0.7}],
        }
    )

    assert response["summary"]["profile_cards"]
    assert len(api.store.profile_cards) == 1


def test_api_job_context_respects_represented_user_scope() -> None:
    api = _api()
    source_id = _ingest_source(api.store)
    job = JobService(api.store).submit("digest_via_fastreact", {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": source_id}]})

    no_rep = api.job_context(job.job_id)

    assert no_rep["source_items"][0]["source_item_id"] == source_id


def test_api_lease_complete_and_fail_job_contract() -> None:
    api = _api()
    source_id = _ingest_source(api.store)
    leased_job = JobService(api.store).submit("digest_via_fastreact", {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": source_id}]})

    lease = api.lease_job(leased_job.job_id, {"worker_id": "fastreact-worker", "lease_seconds": 120})

    assert lease["job"]["status"] == "running"
    assert lease["job"]["worker_id"] == "fastreact-worker"
    assert lease["context"]["source_items"][0]["source_item_id"] == source_id
    assert "pska_write_candidates" in lease["allowed_tools"]
    completed = api.complete_job(leased_job.job_id, {"result": {"ok": True}})
    assert completed["job"]["status"] == "succeeded"

    failed_job = JobService(api.store).submit("digest_via_fastreact", {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": source_id}]}, max_attempts=1)
    api.lease_job(failed_job.job_id, {"worker_id": "fastreact-worker", "lease_seconds": 120})
    failed = api.fail_job(failed_job.job_id, {"error": "digest failed", "retryable": False})

    assert failed["job"]["status"] == "failed"
    assert "digest failed" in failed["job"]["error"]


def test_fastreact_job_writes_returned_candidates_and_event() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))
    service = JobService(
        store,
        fastreact=FakeFastreact(
            {
                "run_id": "run_extract_candidates",
                "source_refs": [{"source_item_id": source_id}],
                "entities": [{"entity_type": "project", "label": "PSKA"}],
                "memory_candidates": [
                    {"kind": "agent_memory", "layer": "semantic", "text": "Fastreact executes PSKA extraction loops.", "confidence": 0.8}
                ],
            }
        ),
    )
    job = service.submit(EXTRACT_VIA_FASTREACT, {"owner_user_id": "user_primary"}, max_attempts=1)

    completed = service.run_next()

    assert completed.status == "succeeded"
    assert completed.result["candidate_write"]["entities"]
    assert completed.result["candidate_write"]["agent_memories"]
    assert len(store.entities) == 1
    assert len(store.agent_memories) == 1
    assert "candidates_written" in [event.event_type for event in store.list_job_events(job.job_id)]


def test_fastreact_job_fails_when_candidate_tool_failed() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))
    service = JobService(
        store,
        fastreact=FakeFastreact(
            {
                "run_id": "run_digest_candidate_error",
                "content": "Producing grounded candidates.",
                "source_refs": [{"source_item_id": source_id}],
                "events": [
                    {"sequence": 10, "type": "tool_call", "tool_name": "pska_pska_write_candidates", "content": ""},
                    {
                        "sequence": 11,
                        "type": "tool_result",
                        "tool_name": "pska_pska_write_candidates",
                        "content": "[MCP_ERROR] KeyError: 'label'",
                    },
                ],
            }
        ),
    )
    job = service.submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary"}, max_attempts=1)

    completed = service.run_next()

    assert completed.status == "failed"
    assert "Fastreact candidate write tool failed" in (completed.error or "")
    assert "KeyError" in (completed.error or "")
    assert not store.review_items
    assert not store.entities
    trace = [event for event in store.list_job_events(job.job_id) if event.event_type == "fastreact_trace"]
    assert trace
    assert trace[0].detail["candidate_tool_errors"]


def test_fastreact_job_prompt_restricts_host_tools() -> None:
    store = _store_with_source()
    fastreact = CapturingFastreact({"run_id": "run_prompt", "content": "ok"})
    service = JobService(store, fastreact=fastreact)
    service.submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary"}, max_attempts=1)

    completed = service.run_next()

    assert completed.status == "succeeded"
    prompt = "\n".join(message["content"] for message in fastreact.kwargs["messages"])
    assert "Use only PSKA MCP tools" in prompt
    assert "exec" in prompt
    assert "read_file" in prompt
    assert "entity_type" in prompt
    assert "label" in prompt
    assert fastreact.kwargs["temperature"] == 0.3
    assert fastreact.kwargs["top_p"] == 0.9
    assert fastreact.kwargs["max_tokens"] == 4096
    assert "metadata" not in fastreact.kwargs


def test_fastreact_digest_generation_options_can_be_overridden() -> None:
    store = _store_with_source()
    fastreact = CapturingFastreact({"run_id": "run_generation_options", "content": "ok"})
    service = JobService(store, fastreact=fastreact)
    service.submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": "user_primary",
            "generation_options": {
                "model": "deepseek-v4-flash",
                "temperature": 0.7,
                "top_p": 0.95,
                "max_tokens": 8192,
            },
        },
        max_attempts=1,
    )

    completed = service.run_next()

    assert completed.status == "succeeded"
    assert fastreact.kwargs["model"] == "deepseek-v4-flash"
    assert fastreact.kwargs["temperature"] == 0.7
    assert fastreact.kwargs["top_p"] == 0.95
    assert fastreact.kwargs["max_tokens"] == 8192


def test_write_candidates_persists_claims_digest_notes_and_derives_triple_hyperedge() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))

    summary = CandidateWriteService(store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "job_id": "job_digest",
            "source_refs": [{"source_item_id": source_id}],
            "knowledge_claims": [
                {
                    "claim_type": "relationship",
                    "statement": "PSKA 依赖 Fastreact 执行 agentic digest。",
                    "subject": "PSKA",
                    "predicate": "depends_on",
                    "object": "Fastreact",
                    "evidence_text": "PSKA depends on Fastreact for agentic service loops.",
                    "confidence": 0.84,
                    "source_refs": [{"source_item_id": source_id}],
                }
            ],
            "digest_notes": [
                {
                    "title": "PSKA digest 依赖",
                    "synopsis": "这份资料说明 PSKA 使用 Fastreact 来执行 agentic digest。",
                    "key_points": [{"summary": "PSKA digest 依赖 Fastreact。", "source_refs": [{"source_item_id": source_id}]}],
                    "actions": [{"summary": "检查 Fastreact worker 是否在线。", "source_refs": [{"source_item_id": source_id}]}],
                    "open_questions": [{"summary": "是否需要 fallback worker？", "source_refs": [{"source_item_id": source_id}]}],
                    "risks": [{"summary": "Fastreact 离线会影响 digest 写回。", "source_refs": [{"source_item_id": source_id}]}],
                    "source_refs": [{"source_item_id": source_id}],
                }
            ],
        }
    )

    assert summary["knowledge_claims"]
    assert summary["digest_notes"]
    assert summary["hyperedges"]
    assert summary["saved_candidates"] >= 3
    claim = store.list_knowledge_claims(owner_user_id="user_primary")[0]
    note = store.list_digest_notes(owner_user_id="user_primary")[0]
    assert claim.statement == "PSKA 依赖 Fastreact 执行 agentic digest。"
    assert note.actions[0]["source_refs"][0]["source_item_id"] == source_id


def test_digest_note_accepts_common_llm_readable_item_fields() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))

    summary = CandidateWriteService(store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source_id}],
            "digest_notes": [
                {
                    "title": "PSKA/FastReAct Integration Manual",
                    "synopsis": "Document describing how FastReAct and PSKA integrate.",
                    "key_points": [{"point": "FastReAct is a headless agentic service layer."}],
                    "actions": [{"action": "Check the service token configuration."}],
                    "open_questions": [{"question": "Which PSKA MCP tools are exposed?"}],
                    "risks": [{"risk": "Service token mismatch can block the integration."}],
                    "relationship_suggestions": [{"why_it_matters": "This links PSKA to FastReAct operationally."}],
                    "source_refs": [{"source_item_id": source_id}],
                }
            ],
        }
    )

    note = store.list_digest_notes(owner_user_id="user_primary")[0]
    assert summary["digest_notes"]
    assert note.key_points[0]["summary"] == "FastReAct is a headless agentic service layer."
    assert note.open_questions[0]["summary"] == "Which PSKA MCP tools are exposed?"
    assert note.risks[0]["source_refs"][0]["source_item_id"] == source_id
    assert note.relationship_suggestions[0]["summary"] == "This links PSKA to FastReAct operationally."


def test_knowledge_claim_requires_evidence_and_low_confidence_claim_requires_review() -> None:
    store = _store_with_source()
    source_id = next(iter(store.source_items))

    try:
        CandidateWriteService(store).write_candidates(
            {
                "schema_version": "pska.candidates.v1",
                "owner_user_id": "user_primary",
                "source_refs": [{"source_item_id": source_id}],
                "knowledge_claims": [{"claim_type": "fact", "statement": "PSKA has a fact."}],
            }
        )
    except CandidateWriteError as exc:
        assert "evidence_text" in str(exc)
    else:
        raise AssertionError("expected CandidateWriteError")

    summary = CandidateWriteService(store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source_id}],
            "knowledge_claims": [
                {
                    "claim_type": "fact",
                    "statement": "PSKA 可能偏好长答案。",
                    "evidence_text": "Maybe PSKA prefers very long answers.",
                    "confidence": 0.4,
                }
            ],
        }
    )

    review = store.get_review_item(summary["review_items"][0])
    assert summary["knowledge_claims"] == []
    assert review.review_type == ReviewType.LOW_CONFIDENCE
    assert review.proposal["plain_text_summary"] == "PSKA 可能偏好长答案。"


def test_digest_logs_surface_events_claims_and_notes() -> None:
    api = _api()
    source_id = _ingest_source(api.store)
    job = JobService(api.store).submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": source_id}]})
    summary = CandidateWriteService(api.store).write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": "user_primary",
            "job_id": job.job_id,
            "source_refs": [{"source_item_id": source_id}],
            "knowledge_claims": [
                {
                    "claim_type": "fact",
                    "statement": "Digest 日志需要可视化。",
                    "evidence_text": "digest task log visualization",
                    "confidence": 0.8,
                }
            ],
            "digest_notes": [
                {
                    "title": "Digest log",
                    "synopsis": "Digest 任务产生了可视化日志。",
                    "source_refs": [{"source_item_id": source_id}],
                }
            ],
        }
    )
    api.store.add_job_event(job.job_id, "candidates_written", "Wrote candidates", summary)

    payload = api.digest_logs(owner_user_id="user_primary")

    assert payload["logs"][0]["job_id"] == job.job_id
    assert payload["logs"][0]["candidate_summary"]["knowledge_claims"] == 1
    assert payload["logs"][0]["candidate_summary"]["digest_notes"] == 1
    assert payload["summary"]["candidate_totals"]["knowledge_claims"] == 1
    assert payload["summary"]["candidate_totals"]["digest_notes"] == 1
    assert payload["summary"]["recent_claims"][0]["statement"] == "Digest 日志需要可视化。"
    assert payload["summary"]["recent_digest_notes"][0]["title"] == "Digest log"
    assert payload["summary"]["has_useful_output"] is True


class FakeFastreact:
    def __init__(self, response: dict) -> None:
        self.response = response

    def ready(self) -> dict:
        return {"ok": True}

    def chat_completion(self, **_kwargs) -> dict:
        return self.response


class CapturingFastreact(FakeFastreact):
    def __init__(self, response: dict) -> None:
        super().__init__(response)
        self.kwargs = {}

    def chat_completion(self, **kwargs) -> dict:
        self.kwargs = kwargs
        return self.response


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    return store


def _store_with_source() -> InMemoryKnowledgeStore:
    store = _store()
    _ingest_source(store)
    return store


def _ingest_source(store: InMemoryKnowledgeStore) -> str:
    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "candidate-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": Visibility.PRIVATE.value,
            "title": "Candidate note",
            "content": {"text": "PSKA depends on Fastreact for agentic service loops."},
        }
    )
    return item.source_item_id


def _api() -> PSKAApi:
    api = object.__new__(PSKAApi)
    api.store = _store()
    api.retrieval = RetrievalService(api.store, ACLService(api.store))
    api.reviews = ReviewService(api.store)
    api.candidates = CandidateWriteService(api.store)
    return api
