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
    store.add_user(User("user_secondary", "secondary", UserRole.USER))
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
    assert response.gaps == ["ungrounded_graph_context"]
    assert response.score_debug["graph_context_used"] is True
    assert response.score_debug["diagnostics"]["ungrounded_graph_edges"] == 1
    assert response.hypergraph_context[0]["relation_type"] == "recommended"
    assert {member["label"] for member in response.hypergraph_context[0]["members"]} == {"A", "B"}


def test_hypergraph_context_returns_grounded_source_citations() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "twitter",
            "record_type": "tweet",
            "source_id": "tweet_graphrag",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "GraphRAG tweet",
            "url": "https://x.com/u/status/tweet_graphrag",
            "content": {"text": "PSKA should use GraphRAG for grounded personal knowledge retrieval."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_pska", "project", "PSKA", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_graphrag", "concept", "GraphRAG", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="uses",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_pska", "system"), ("ent_graphrag", "retrieval_pattern")],
        evidence_text="PSKA should use GraphRAG for grounded personal knowledge retrieval.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.8,
    )

    response = RetrievalService(store, ACLService(store)).search("PSKA GraphRAG 关系", store.get_user("user_primary"))
    edge_context = response.hypergraph_context[0]

    assert edge_context["relation_type"] == "uses"
    assert edge_context["source_refs"][0]["source_item_id"] == source.source_item_id
    assert edge_context["evidence_citations"][0]["source_item_id"] == source.source_item_id
    assert edge_context["evidence_citations"][0]["chunk_id"].startswith("chk_")
    assert "GraphRAG" in edge_context["evidence_citations"][0]["snippet"]


def test_retrieval_reports_conflicting_graph_relations() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_conflict",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Conflict note",
            "content": {"text": "Claim A contradicts Claim B."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_claim_a", "claim", "Claim A", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_claim_b", "claim", "Claim B", "user_primary", "private_primary", Visibility.PRIVATE))
    edge = graph.create_hyperedge(
        relation_type="contradicts",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        members=[("ent_claim_a", "left"), ("ent_claim_b", "right")],
        evidence_text="Claim A contradicts Claim B.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.9,
    )

    response = RetrievalService(store, ACLService(store)).search("Claim A Claim B", store.get_user("user_primary"))

    assert response.gaps == []
    assert response.conflicts == [f"graph_conflict:{edge.hyperedge_id}:contradicts"]
    assert response.score_debug["diagnostics"]["conflict_count"] == 1


def test_graph_paths_return_two_hop_grounded_relation_chain() -> None:
    store = make_store()
    source_fastreact = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_pska_fastreact",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "PSKA FastReAct boundary",
            "content": {"text": "PSKA delegates complex agentic work to FastReAct."},
        }
    )
    source_digest = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_fastreact_digest",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "FastReAct digest worker",
            "content": {"text": "FastReAct executes digest loops for PSKA."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_pska", "project", "PSKA", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_fastreact", "service", "FastReAct", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_digest", "workflow", "Digest", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="delegates_to",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_pska", "caller"), ("ent_fastreact", "executor")],
        evidence_text="PSKA delegates complex agentic work to FastReAct.",
        source_refs=[SourceRef(source_item_id=source_fastreact.source_item_id)],
        confidence=0.9,
    )
    graph.create_hyperedge(
        relation_type="executes",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_fastreact", "executor"), ("ent_digest", "workflow")],
        evidence_text="FastReAct executes digest loops for PSKA.",
        source_refs=[SourceRef(source_item_id=source_digest.source_item_id)],
        confidence=0.8,
    )

    response = RetrievalService(store, ACLService(store)).search("PSKA 到 Digest 的关系路径", store.get_user("user_primary"))

    two_hop_paths = [path for path in response.graph_paths if path["depth"] == 2]
    assert two_hop_paths
    path = two_hop_paths[0]
    assert [entity["label"] for entity in path["entities"]] == ["PSKA", "FastReAct", "Digest"]
    assert [edge["relation_type"] for edge in path["edges"]] == ["delegates_to", "executes"]
    assert path["explanation"] == "PSKA -[delegates_to]-> FastReAct -[executes]-> Digest"
    assert path["score"] > 0
    assert path["score_debug"]["evidence_coverage"] == 1.0
    assert path["edges"][0]["evidence_citations"][0]["source_item_id"] == source_fastreact.source_item_id
    assert path["edges"][1]["evidence_citations"][0]["source_item_id"] == source_digest.source_item_id
    assert response.score_debug["graph_paths_used"] is True


def test_graph_paths_rank_grounded_high_confidence_paths_first() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_grounded_path",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Grounded graph path",
            "content": {"text": "PSKA has strong evidence for the grounded path."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_pska_rank", "project", "PSKA", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_grounded_rank", "concept", "Grounded", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_weak_rank", "concept", "Weak", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="supports",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        members=[("ent_pska_rank", "system"), ("ent_weak_rank", "claim")],
        evidence_text="Weak ungrounded relation.",
        confidence=0.1,
    )
    graph.create_hyperedge(
        relation_type="supports",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        members=[("ent_pska_rank", "system"), ("ent_grounded_rank", "claim")],
        evidence_text="PSKA has strong evidence for the grounded path.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.95,
    )

    response = RetrievalService(store, ACLService(store)).search("PSKA graph paths", store.get_user("user_primary"))

    grounded_path = next(path for path in response.graph_paths if path["entities"][-1]["label"] == "Grounded")
    weak_path = next(
        path
        for path in response.graph_paths
        if path["depth"] == 1 and [entity["label"] for entity in path["entities"]] == ["PSKA", "Weak"]
    )
    assert response.graph_paths.index(grounded_path) < response.graph_paths.index(weak_path)
    assert grounded_path["score"] > weak_path["score"]
    assert grounded_path["score_debug"]["evidence_coverage"] == 1.0
    assert weak_path["score_debug"]["evidence_coverage"] == 0.0


def test_graph_paths_link_entities_by_alias_metadata() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_fr_digest_alias",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "FR digest note",
            "content": {"text": "FR runs digest jobs for PSKA."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(
        Entity(
            "ent_fastreact_alias",
            "service",
            "FastReAct",
            "user_primary",
            "private_primary",
            Visibility.PRIVATE,
            metadata={"aliases": ["FR", "Fast React", "FastReact"]},
        )
    )
    graph.create_entity(Entity("ent_digest_alias", "workflow", "Digest", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="executes",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_fastreact_alias", "executor"), ("ent_digest_alias", "workflow")],
        evidence_text="FR runs digest jobs for PSKA.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.85,
    )

    response = RetrievalService(store, ACLService(store)).search("FR digest 关系", store.get_user("user_primary"))

    assert response.graph_paths
    assert response.graph_paths[0]["seed"]["label"] == "FastReAct"
    assert response.graph_paths[0]["edges"][0]["relation_type"] == "executes"
    assert response.graph_paths[0]["edges"][0]["evidence_citations"][0]["source_item_id"] == source.source_item_id


def test_entity_linking_uses_word_boundaries_for_short_latin_labels() -> None:
    store = make_store()
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_ai", "concept", "AI", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_digest_boundary", "workflow", "Digest", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="related_to",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        members=[("ent_ai", "concept"), ("ent_digest_boundary", "workflow")],
        evidence_text="AI is related to digest workflows.",
    )

    response = RetrievalService(store, ACLService(store)).search("explain daily routine", store.get_user("user_primary"))

    assert response.graph_paths == []
    assert response.hypergraph_context == []


def test_hypergraph_context_does_not_leak_private_evidence_citations() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "twitter",
            "record_type": "tweet",
            "source_id": "tweet_private_graph",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Private graph evidence",
            "content": {"text": "Private GraphRAG evidence belongs only to user_primary."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_public_a", "concept", "PublicA", "user_primary", "private_primary", Visibility.PUBLIC))
    graph.create_entity(Entity("ent_public_b", "concept", "PublicB", "user_primary", "private_primary", Visibility.PUBLIC))
    graph.create_hyperedge(
        relation_type="related_to",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PUBLIC,
        directionality=Directionality.UNDIRECTED,
        members=[("ent_public_a", "left"), ("ent_public_b", "right")],
        evidence_text="The relation is public, but its source evidence is private.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.8,
    )

    response = RetrievalService(store, ACLService(store)).search("PublicA PublicB 关系", store.get_user("user_secondary"))

    assert response.hypergraph_context
    assert response.hypergraph_context[0]["source_refs"] == []
    assert response.hypergraph_context[0]["evidence_citations"] == []


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
