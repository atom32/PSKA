from __future__ import annotations

import json

from pska_core.adapters.conversation import conversation_to_payload
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.models import User
from pska_core.store import InMemoryKnowledgeStore
from tests.fakes import FakeLLM, extraction_response


class FakeAgenticExtractionService:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = []

    def ready(self) -> dict:
        return {"ok": True, "provider": "test", "adapter": "fake"}

    def search(
        self,
        query,
        user,
        *,
        represented_user_id=None,
        max_iterations=3,
        skills=None,
        tool_policy=None,
        session_id=None,
    ):
        self.calls.append(
            {
                "query": query,
                "user_id": user.user_id,
                "tenant_id": user.tenant_id,
                "represented_user_id": represented_user_id,
                "max_iterations": max_iterations,
                "skills": skills,
                "tool_policy": tool_policy,
                "session_id": session_id,
            }
        )
        return {"answer": json.dumps(self.response)}


def test_extraction_creates_entities_hyperedges_and_review_items() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "extract-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {
                "text": (
                    "Project Atlas depends on the Twitter Archive channel. "
                    "The policy P-204 covers the education enrollment stage for dependent K. "
                    "The Review Agent must confirm any team-visible sharing before release."
                )
            },
        }
    )

    llm = FakeLLM([extraction_response()])
    report = ExtractionService(store, llm=llm).extract_source_item(item)

    labels = {entity.label for entity in store.list_entities()}
    relations = {edge.relation_type for edge in store.hyperedges.values()}
    assert {"Project Atlas", "Twitter Archive", "P-204", "dependent K", "education enrollment"} <= labels
    assert {"covers", "depends_on", "requires_review"} <= relations
    assert report.review_items_created
    assert "knowledge extraction agent" in llm.prompts[0]["system"]
    assert "Chinese" in llm.prompts[0]["system"]
    assert "Prefer Chinese" in llm.prompts[0]["prompt"]


def test_extraction_defaults_to_agentic_service_without_tools() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "agentic-extract-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "Project Atlas depends on the Twitter Archive channel."},
        }
    )
    agentic = FakeAgenticExtractionService(extraction_response())

    report = ExtractionService(store, agentic_service=agentic).extract_source_item(item)

    assert report.hyperedges_created
    assert agentic.calls
    assert agentic.calls[0]["max_iterations"] == 1
    assert agentic.calls[0]["skills"] == []
    assert agentic.calls[0]["tool_policy"] == {"mode": "none"}
    assert "Return exactly one strict JSON object" in agentic.calls[0]["query"]


def test_extraction_repairs_one_member_hyperedge_schema() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "repair-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "Project Atlas depends on Twitter Archive."},
        }
    )
    invalid = {
        "entities": [{"entity_type": "project", "label": "Project Atlas"}],
        "hyperedges": [
            {
                "relation_type": "depends_on",
                "directionality": "directed",
                "evidence_text": "Project Atlas depends on Twitter Archive.",
                "confidence": 0.8,
                "members": [{"entity_type": "project", "label": "Project Atlas", "role": "subject"}],
            }
        ],
        "review_items": [],
    }
    llm = FakeLLM([invalid, extraction_response()])

    report = ExtractionService(store, llm=llm).extract_source_item(item)

    assert report.hyperedges_created
    assert len(llm.prompts) == 2
    assert "schema correction agent" in llm.prompts[1]["system"]
    assert "Chinese" in llm.prompts[1]["system"]


def test_conversation_review_item_preserves_message_provenance() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    payload = conversation_to_payload(
        {
            "id": "conv_extract",
            "messages": [
                {
                    "id": "msg_pref",
                    "role": "user",
                    "participant_name": "Dawei",
                    "content": "Please remember that I prefer short answers.",
                    "created_at": "2026-06-11T11:00:00Z",
                }
            ],
        },
        owner_user_id="user_primary",
        space_id="private_primary",
    )
    item = IngestService(store).ingest_channel_payload(payload)
    llm = FakeLLM(
        [
            {
                "entities": [],
                "hyperedges": [],
                "review_items": [
                    {
                        "review_type": "profile_update",
                        "title": "Remember answer length preference",
                        "proposal": {
                            "profile_delta": {"answer_style": "short"},
                            "message_ids": ["msg_pref"],
                        },
                    }
                ],
            }
        ]
    )

    report = ExtractionService(store, llm=llm).extract_source_item(item)

    assert report.review_items_created
    review = store.review_items[report.review_items_created[0]]
    assert review.proposal["profile_delta"] == {"answer_style": "short"}
    assert any(ref["message_id"] == "msg_pref" for ref in review.proposal["source_refs"])
    assert "proposal.message_ids" in llm.prompts[0]["prompt"]


def test_extraction_prompt_and_claim_dedupe_key_are_stable_across_retries() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "claim-dedupe-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "PSKA depends on FastReAct for digest jobs."},
        }
    )
    llm = FakeLLM(
        [
            {
                "knowledge_claims": [
                    {
                        "claim_type": "fact",
                        "dedupe_key": "pska:depends_on:fastreact",
                        "statement": "PSKA 依赖 FastReAct 执行 digest。",
                        "subject": "PSKA",
                        "predicate": "depends_on",
                        "object": "FastReAct",
                        "qualifiers": {"notability": "medium"},
                        "evidence_text": "PSKA depends on FastReAct for digest jobs.",
                        "confidence": 0.82,
                    }
                ],
                "entities": [],
                "hyperedges": [],
                "review_items": [],
            },
            {
                "knowledge_claims": [
                    {
                        "claim_type": "fact",
                        "dedupe_key": "PSKA:DEPENDS_ON:FASTREACT",
                        "statement": "FastReAct 是 PSKA digest 的执行层。",
                        "subject": "PSKA",
                        "predicate": "depends_on",
                        "object": "FastReAct",
                        "qualifiers": {"notability": "medium"},
                        "evidence_text": "PSKA depends on FastReAct for digest jobs.",
                        "confidence": 0.9,
                    }
                ],
                "entities": [],
                "hyperedges": [],
                "review_items": [],
            },
        ]
    )
    service = ExtractionService(store, llm=llm)

    first = service.extract_source_item(item)
    second = service.extract_source_item(item)

    assert first.knowledge_claims_created == second.knowledge_claims_created
    claims = store.list_knowledge_claims(owner_user_id="user_primary")
    assert len(claims) == 1
    assert claims[0].statement == "FastReAct 是 PSKA digest 的执行层。"
    assert claims[0].metadata["dedupe_key"] == "pska:depends_on:fastreact"
    assert claims[0].metadata["prompt_version"] == "pska.extraction.v2"
    assert "Prompt version: pska.extraction.v2" in llm.prompts[0]["prompt"]
    assert "<source_text>" in llm.prompts[0]["prompt"]


def test_extraction_tolerates_common_llm_schema_drift() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "schema-drift-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "Alice shipped the Acme MVP quickly."},
        }
    )
    llm = FakeLLM(
        [
            {
                "knowledge_claims": [
                    {
                        "claim_type": "fact",
                        "statement": "Alice shipped the Acme MVP quickly.",
                        "evidence_text": "Alice shipped the Acme MVP quickly.",
                        "confidence": "high",
                    }
                ],
                "entities": [
                    {"entity_type": "person", "label": "Alice"},
                    {"entity_type": "company", "label": "Acme"},
                ],
                "hyperedges": [
                    {
                        "relation_type": "shipped",
                        "directionality": "directed",
                        "evidence_text": "Alice shipped the Acme MVP quickly.",
                        "confidence": "high",
                        "members": [
                            {"entity_type": "person", "label": "Alice", "role": "shipper"},
                            {"entity_type": "company", "label": "Acme", "role": "product"},
                        ],
                    }
                ],
                "review_items": [
                    {
                        "review_type": "memory_candidate",
                        "title": "Remember Acme shipping note",
                        "proposal": "Alice shipped the Acme MVP quickly.",
                    }
                ],
            }
        ]
    )

    report = ExtractionService(store, llm=llm).extract_source_item(item)

    assert report.knowledge_claims_created
    assert report.hyperedges_created
    assert report.review_items_created
    claim = store.list_knowledge_claims(owner_user_id="user_primary")[0]
    review = store.review_items[report.review_items_created[0]]
    assert claim.confidence == 0.9
    assert review.proposal["summary"] == "Alice shipped the Acme MVP quickly."
