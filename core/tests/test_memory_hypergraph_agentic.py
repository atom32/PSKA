from __future__ import annotations

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.ingest import IngestService
from pska_core.memory import MemoryService
from pska_core.models import Entity, SourceRef, User
from pska_core.retrieval import RetrievalService
from pska_core.store import InMemoryKnowledgeStore
from tests.fakes import FakeLLM, agentic_answer_response, agentic_plan_response


def make_store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    return store


def test_agent_memory_belongs_to_represented_user() -> None:
    store = make_store()
    memory = MemoryService(store).write_agent_memory(
        owner_user_id="user_primary",
        layer=MemoryLayer.SEMANTIC,
        text="Prefers concise answers.",
        confidence=0.9,
        source_refs=[SourceRef(message_id="msg_1")],
        created_by_user_id="agent_service",
    )

    assert memory.owner_user_id == "user_primary"
    assert memory.created_by_user_id == "agent_service"


def test_high_sensitive_profile_update_creates_review_item() -> None:
    store = make_store()
    result = MemoryService(store).propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"taboos": ["private topic"]},
        source_refs=[SourceRef(message_id="msg_2")],
        sensitivity="high",
    )

    assert result.review_type == ReviewType.PROFILE_UPDATE
    assert result.status == "pending"
    assert store.review_items[result.review_item_id] == result


def test_binary_relation_is_size_two_hyperedge() -> None:
    store = make_store()
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_a", "person", "A", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_b", "book", "B", "user_primary", "private_primary", Visibility.PRIVATE))

    edge = graph.create_hyperedge(
        relation_type="recommended",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_a", "recommender"), ("ent_b", "object")],
        evidence_text="A recommended B.",
    )

    members = [member for member in store.hyperedge_members if member.hyperedge_id == edge.hyperedge_id]
    assert len(members) == 2
    assert {member.role for member in members} == {"recommender", "object"}


def test_multi_party_relation_preserves_member_roles_and_ambiguous_direction() -> None:
    store = make_store()
    graph = HypergraphService(store)
    for entity_id, entity_type in [("ent_policy", "policy"), ("ent_person", "person"), ("ent_stage", "event")]:
        graph.create_entity(Entity(entity_id, entity_type, entity_id, "user_primary", "private_primary", Visibility.PRIVATE))

    edge = graph.create_hyperedge(
        relation_type="covers",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        members=[("ent_policy", "policy"), ("ent_person", "beneficiary"), ("ent_stage", "stage")],
        evidence_text="Original text retained for direction judgment.",
    )

    assert edge.directionality == Directionality.AMBIGUOUS
    members = [member for member in store.hyperedge_members if member.hyperedge_id == edge.hyperedge_id]
    assert len(members) == 3
    assert {member.role for member in members} == {"policy", "beneficiary", "stage"}


def test_agentic_search_returns_trace_and_citations() -> None:
    store = make_store()
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note-agentic",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "agentic search should cite this source"},
        }
    )
    retrieval = RetrievalService(store, ACLService(store))
    llm = FakeLLM([
        agentic_plan_response("agentic source"),
        agentic_answer_response("The note cites agentic search evidence."),
    ])
    response = AgenticSearchService(retrieval, llm=llm).search("agentic source", store.get_user("user_primary"))

    assert response.trace.retrieval_plan[0] == "acl_filter"
    assert response.retrieval.citations
    assert response.trace.evidence_check == "has_citations"
    assert response.answer == "The note cites agentic search evidence."
    assert len(llm.prompts) == 2
