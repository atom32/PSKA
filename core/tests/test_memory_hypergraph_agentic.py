from __future__ import annotations

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.ingest import IngestService
from pska_core.memory import MemoryService
from pska_core.models import Entity, SourceRef, User
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
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


def test_agent_memory_can_be_verified_and_forgotten() -> None:
    store = make_store()
    service = MemoryService(store)
    memory = service.write_agent_memory(
        owner_user_id="user_primary",
        layer=MemoryLayer.EPISODIC,
        text="Temporary preference.",
        confidence=0.3,
        source_refs=[SourceRef(message_id="msg_lifecycle")],
        created_by_user_id="agent_service",
        decay_policy="decay",
    )

    verified = service.verify_agent_memory(memory.agent_memory_id, confidence=0.85, decay_policy="manual")
    assert verified.confidence == 0.85
    assert verified.decay_policy == "manual"
    assert verified.last_verified_at is not None

    forgotten = service.forget_agent_memory(memory.agent_memory_id)
    assert forgotten.confidence == 0.0
    assert forgotten.decay_policy == "forgotten"


def test_sensitive_profile_update_proposal_applies_with_confidence() -> None:
    store = make_store()
    service = MemoryService(store)
    review = service.propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"communication": {"style": "concise"}},
        source_refs=[SourceRef(message_id="msg_profile")],
        sensitivity="sensitive",
        confidence=0.7,
    )

    assert review.review_type == ReviewType.PROFILE_UPDATE
    assert review.proposal["confidence"] == 0.7
    assert review.proposal["source_refs"][0]["message_id"] == "msg_profile"

    ReviewService(store).approve_and_apply(review.review_item_id, actor_user_id="user_primary")
    card = next(iter(store.profile_cards.values()))
    assert card.profile == {"communication": {"style": "concise"}}
    assert card.confidence == 0.7
    assert card.source_refs == [SourceRef(message_id="msg_profile")]


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
    assert result.proposal["source_refs"][0]["message_id"] == "msg_2"
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


def test_graph_global_query_returns_visible_hypergraph_context_without_chunk_hit() -> None:
    store = make_store()
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_a", "person", "A", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_b", "book", "B", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="recommended",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_a", "recommender"), ("ent_b", "object")],
        evidence_text="A recommended B.",
    )

    response = RetrievalService(store, ACLService(store)).search("列出重要实体和关系", store.get_user("user_primary"))

    assert response.results == []
    assert response.gaps == []
    assert response.score_debug["graph_context_used"] is True
    assert response.hypergraph_context[0]["relation_type"] == "recommended"
    assert {member["label"] for member in response.hypergraph_context[0]["members"]} == {"A", "B"}


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
