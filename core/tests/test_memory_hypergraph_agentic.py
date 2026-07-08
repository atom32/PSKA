from __future__ import annotations

import sys
import types

from pska_core.acl import ACLService
from pska_core.agentic_service import AgenticServiceConfig, FastreactAgenticServiceAdapter
from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, Visibility
from pska_core.hipporag_index import HippoRAGOfflineIndex
from pska_core.hypergraph import HypergraphService
from pska_core.ingest import IngestService
from pska_core.memory import MemoryService
from pska_core.models import Entity, SourceRef, User
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.store import InMemoryKnowledgeStore


def make_store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("user_secondary", "secondary", UserRole.USER))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    return store


class HippoFixtureEmbeddingProvider:
    provider_name = "fixture-hippo"
    model_name = "fixture-hippo-model"
    dimensions = 2

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lower = text.lower()
            if "semanticquery" in lower or "latent bridge" in lower or "semantic target" in lower:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


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
    assert result.proposal["plain_text_summary"] == "Profile update requires human review."
    assert store.review_items[result.review_item_id] == result


def test_retrieval_includes_memory_context_with_source_citations() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_memory_context",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Memory source",
            "content": {"text": "The user prefers concise PSKA answers."},
        }
    )
    MemoryService(store).write_agent_memory(
        owner_user_id="user_primary",
        layer=MemoryLayer.SEMANTIC,
        text="User prefers concise PSKA answers.",
        confidence=0.9,
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        created_by_user_id="agent_service",
    )

    response = RetrievalService(store, ACLService(store)).search("concise PSKA preference", store.get_user("user_primary"))

    assert response.memory_context_used is True
    assert response.memory_context[0]["text"] == "User prefers concise PSKA answers."
    assert response.memory_context[0]["citations"][0]["source_item_id"] == source.source_item_id
    assert response.score_debug["diagnostics"]["memory_context_count"] == 1


def test_retrieval_includes_profile_context_with_source_citations() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_profile_context",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Profile source",
            "content": {"text": "Communication style should stay concise."},
        }
    )
    MemoryService(store).propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"communication": {"style": "concise"}},
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.8,
    )

    response = RetrievalService(store, ACLService(store)).search("profile communication style", store.get_user("user_primary"))

    assert response.profile_context_used is True
    assert response.profile_context[0]["profile"] == {"communication": {"style": "concise"}}
    assert response.profile_context[0]["citations"][0]["source_item_id"] == source.source_item_id
    assert response.score_debug["diagnostics"]["profile_context_count"] == 1


def test_agent_service_memory_context_requires_represented_user() -> None:
    store = make_store()
    MemoryService(store).write_agent_memory(
        owner_user_id="user_primary",
        layer=MemoryLayer.SEMANTIC,
        text="User prefers concise answers.",
        confidence=0.9,
        source_refs=[],
        created_by_user_id="agent_service",
    )
    retrieval = RetrievalService(store, ACLService(store))

    denied = retrieval.search("concise preference", store.get_user("agent_service"))
    allowed = retrieval.search("concise preference", store.get_user("agent_service"), represented_user_id="user_primary")

    assert denied.memory_context == []
    assert allowed.memory_context
    assert allowed.request_user_id == "user_primary"


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


def test_retrieval_ppr_expands_to_graph_connected_chunks() -> None:
    store = make_store()
    seed_source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_project_a_seed",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "ProjectA seed",
            "content": {"text": "ProjectA has a starting point for graph-aware retrieval."},
        }
    )
    hidden_source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_hidden_ppr_evidence",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Unmatched downstream note",
            "content": {"text": "The downstream implementation decision is preserved in this evidence chunk."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_project_a_ppr", "project", "ProjectA", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_downstream_ppr", "decision", "DownstreamDecision", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="depends_on",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_project_a_ppr", "subject"), ("ent_downstream_ppr", "decision")],
        evidence_text="ProjectA depends on the downstream implementation decision.",
        source_refs=[SourceRef(source_item_id=hidden_source.source_item_id)],
        confidence=0.92,
    )

    response = RetrievalService(store, ACLService(store)).search(
        "ProjectA starting point",
        store.get_user("user_primary"),
        top_k=2,
    )

    assert {result.source_item_id for result in response.results} == {seed_source.source_item_id, hidden_source.source_item_id}
    expanded = next(result for result in response.results if result.source_item_id == hidden_source.source_item_id)
    assert expanded.score_debug["graph_expansion"] == 1.0
    assert expanded.score_debug["graph_ppr"] > 0
    assert response.score_debug["graph_ranker"] == "ppr_chunk_entity_fusion"
    assert response.score_debug["graph_ppr_enabled"] is True
    assert response.score_debug["graph_expanded_candidates"] >= 1
    assert response.score_debug["hipporag_offline_graph"]["num_fact_nodes"] == 1
    assert response.score_debug["offline_index_freshness"]["dirty"] >= 2
    assert response.score_debug["offline_index_freshness"]["fallback"] == "request_scoped_rebuild"


def test_hipporag_offline_index_builds_fact_entity_passage_graph() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_hipporag_offline",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Offline HippoRAG note",
            "content": {"text": "ProjectA delegates offline indexing to the GraphBuilder fact layer."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_offline_project", "project", "ProjectA", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_offline_builder", "component", "GraphBuilder", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="delegates_to",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_offline_project", "subject"), ("ent_offline_builder", "object")],
        evidence_text="ProjectA delegates offline indexing to GraphBuilder.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.93,
    )
    chunks = store.list_chunks_for_sources({source.source_item_id})
    index = HippoRAGOfflineIndex.build(
        entities=store.list_entities(),
        hyperedges=store.list_hyperedges_for_entities({"ent_offline_project", "ent_offline_builder"}),
        chunks=chunks,
        item_by_id={source.source_item_id: source},
    )

    assert index.graph_info["num_fact_nodes"] == 1
    assert index.graph_info["num_phrase_nodes"] == 2
    assert index.graph_info["num_passage_nodes"] == 1
    assert index.graph_info["num_fact_to_entity_edges"] == 2
    assert index.graph_info["num_fact_to_passage_edges"] == 1
    top_facts = index.score_facts("ProjectA offline indexing")
    assert top_facts[0]["summary"]["relation_type"] == "delegates_to"
    assert top_facts[0]["score"] > 0


def test_hipporag_offline_index_scores_facts_and_entities_with_embeddings() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_hipporag_embedding",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Embedding HippoRAG note",
            "content": {"text": "Latent bridge evidence points at the semantic target."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_latent_bridge", "concept", "Latent Bridge", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_semantic_target", "concept", "Semantic Target", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="supports",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        members=[("ent_latent_bridge", "subject"), ("ent_semantic_target", "object")],
        evidence_text="Latent bridge evidence points at the semantic target.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.9,
    )
    index = HippoRAGOfflineIndex.build(
        entities=store.list_entities(),
        hyperedges=store.list_hyperedges_for_entities({"ent_latent_bridge", "ent_semantic_target"}),
        chunks=store.list_chunks_for_sources({source.source_item_id}),
        item_by_id={source.source_item_id: source},
    ).with_embeddings(HippoFixtureEmbeddingProvider())
    query_embedding = HippoFixtureEmbeddingProvider().embed_texts(["SemanticQuery"])[0]

    top_facts = index.score_facts("SemanticQuery", query_embedding=query_embedding)
    linked_entities = index.link_entities("SemanticQuery", query_embedding=query_embedding)

    assert top_facts[0]["summary"]["relation_type"] == "supports"
    assert top_facts[0]["score_debug"]["lexical"] == 0
    assert top_facts[0]["score_debug"]["embedding"] > 0.99
    assert linked_entities[0].entity_id in {"ent_latent_bridge", "ent_semantic_target"}
    assert linked_entities[0].score_debug["embedding"] > 0.99


def test_retrieval_uses_hipporag_embedding_linking_for_fact_seeds() -> None:
    store = make_store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_retrieval_embedding_link",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Retrieval embedding link note",
            "content": {"text": "Latent bridge evidence points at the semantic target."},
        }
    )
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_retrieval_latent", "concept", "Latent Bridge", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_retrieval_target", "concept", "Semantic Target", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="supports",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        members=[("ent_retrieval_latent", "subject"), ("ent_retrieval_target", "object")],
        evidence_text="Latent bridge evidence points at the semantic target.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.9,
    )

    response = RetrievalService(
        store,
        ACLService(store),
        embedding_provider=HippoFixtureEmbeddingProvider(),
    ).search("SemanticQuery", store.get_user("user_primary"))

    assert response.score_debug["hipporag_embedding_linking"]["enabled"] is True
    assert response.score_debug["graph_fact_seed_count"] == 1
    assert response.score_debug["graph_ranker"] == "ppr_chunk_entity_fusion"
    assert response.results[0].source_item_id == source.source_item_id


def test_retrieval_without_graph_edges_falls_back_to_plain_rag() -> None:
    store = make_store()
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note_plain_rag",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Plain retrieval note",
            "content": {"text": "Plain RAG should still answer when no graph is available."},
        }
    )

    response = RetrievalService(store, ACLService(store)).search("plain RAG", store.get_user("user_primary"))

    assert response.results
    assert response.score_debug["graph_ranker"] == "rag_fallback"
    assert response.score_debug["graph_ppr_enabled"] is False
    assert response.score_debug["graph_expanded_candidates"] == 0


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


def test_graph_paths_can_use_optional_rapidfuzz_for_alias_typos(monkeypatch) -> None:
    class FakeFuzz:
        @staticmethod
        def WRatio(alias, haystack):
            return 94.0 if alias == "fastreact" and "fastreakt" in haystack else 0.0

    module = types.ModuleType("rapidfuzz")
    module.fuzz = FakeFuzz
    monkeypatch.setitem(sys.modules, "rapidfuzz", module)
    store = make_store()
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_fastreact_fuzzy", "service", "FastReAct", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_digest_fuzzy", "workflow", "Digest", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="executes",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_fastreact_fuzzy", "executor"), ("ent_digest_fuzzy", "workflow")],
        evidence_text="FastReAct executes digest.",
        confidence=0.85,
    )

    response = RetrievalService(store, ACLService(store)).search("FastReakt 关系", store.get_user("user_primary"))

    assert response.graph_paths
    assert response.graph_paths[0]["seed"]["label"] == "FastReAct"


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


def test_external_agentic_adapter_returns_trace_and_citations() -> None:
    store = make_store()

    class FakeFastreactClient:
        def __init__(self):
            self.captured_kwargs = {}

        def ready(self):
            return {"ok": True, "pska_tools_loaded": True}

        def chat_completion(self, **kwargs):
            self.captured_kwargs = kwargs
            return {
                "run_id": "run_agentic",
                "session_id": "session_agentic",
                "answer": "The note cites",
                "retrieval": {
                    "citations": [{"source_item_id": "src_agentic", "chunk_id": "chk_agentic"}],
                    "results": [{"title": "Agentic source", "citation": {"source_item_id": "src_agentic", "chunk_id": "chk_agentic"}}],
                },
                "events": [
                    {"type": "tool_call", "tool_name": "pska_pska_search", "tool_args": {"query": "agentic source"}},
                    {"type": "session_end", "content": "The note cites external agentic evidence."},
                ],
                "tool_calls": [{"tool_name": "pska_pska_search", "tool_args": {"query": "agentic source"}}],
                "metadata": {"event_count": 2},
                "trace": {
                    "retrieval_plan": ["external_agentic_service", "pska_search"],
                    "evidence_check": "has_citations",
                },
            }

    service = FastreactAgenticServiceAdapter(
        AgenticServiceConfig(
            provider="fastreact",
            url="http://agentic.test",
            service_token="token",
            model="deepseek-v4-flash",
            temperature=0.3,
            top_p=0.9,
            max_tokens=4096,
        ),
        client=FakeFastreactClient(),
    )

    response = service.search("agentic source", store.get_user("user_primary"))

    assert response["trace"]["retrieval_plan"][0] == "external_agentic_service"
    assert response["retrieval"]["citations"]
    assert response["trace"]["evidence_check"] == "has_citations"
    assert response["trace"]["run_id"] == "run_agentic"
    assert response["trace"]["event_count"] == 2
    assert response["trace"]["tool_calls"][0]["tool_name"] == "pska_pska_search"
    assert response["trace"]["events"][0]["type"] == "tool_call"
    assert response["answer"] == "The note cites external agentic evidence."
    assert response["agentic_service"]["provider"] == "fastreact"
    prompt = "\n".join(message["content"] for message in service.client.captured_kwargs["messages"])
    assert "HippoRAG-style loop" in prompt
    assert "previous/next passage window" in prompt
    assert "connected entity/fact/claim neighbors" in prompt
    assert service.client.captured_kwargs["model"] == "deepseek-v4-flash"
    assert service.client.captured_kwargs["temperature"] == 0.3
    assert service.client.captured_kwargs["top_p"] == 0.9
    assert service.client.captured_kwargs["max_tokens"] == 4096
    assert service.client.captured_kwargs["tenant_id"] == "tenant_default"
    assert service.client.captured_kwargs["scope"]["tenant_id"] == "tenant_default"


def test_external_agentic_adapter_reads_chat_completion_events() -> None:
    store = make_store()

    class FakeFastreactClient:
        def create_run(self, **kwargs):
            raise AssertionError("PSKA agentic adapter should use /v1/chat/completions, not /v1/runs")

        def chat_completion(self, **kwargs):
            return {
                "run_id": "chat_background",
                "session_id": "session_background",
                "events": [
                    {"type": "session_start", "content": "summarize"},
                    {"type": "tool_call", "tool_call_id": "call_1", "tool_name": "exec", "tool_args": {"command": "date"}},
                    {"type": "tool_result", "tool_call_id": "call_1", "tool_name": "exec", "content": "Fri Jun 19"},
                    {"type": "session_end", "content": "完整回答\\n第二行"},
                ],
                "metadata": {"event_count": 4, "run_protocol": "chat_completion"},
            }

    service = FastreactAgenticServiceAdapter(
        AgenticServiceConfig(provider="fastreact", url="http://agentic.test"),
        client=FakeFastreactClient(),
    )

    response = service.search("使用 bash 命令查看当前时间", store.get_user("user_primary"))

    assert response["answer"] == "完整回答\\n第二行"
    assert response["trace"]["event_count"] == 4
    assert response["trace"]["events"][-1]["type"] == "session_end"
    assert response["trace"]["tool_calls"][0]["tool_name"] == "exec"
    assert response["agentic_service"]["run_id"] == "chat_background"


def test_external_agentic_adapter_reads_final_content_not_preview() -> None:
    store = make_store()
    full_answer = "完整最终答案" * 200

    class FakeFastreactClient:
        def create_run(self, **kwargs):
            raise AssertionError("PSKA agentic adapter should use /v1/chat/completions, not /v1/runs")

        def chat_completion(self, **kwargs):
            return {
                "run_id": "chat_full_content",
                "events": [
                    {
                        "type": "session_end",
                        "final_content": full_answer,
                        "content_preview": "完整最终答案\n[... truncated ...]",
                        "content_truncated": True,
                    }
                ],
            }

    service = FastreactAgenticServiceAdapter(
        AgenticServiceConfig(provider="fastreact", url="http://agentic.test"),
        client=FakeFastreactClient(),
    )

    response = service.search("保存完整答案", store.get_user("user_primary"))

    assert response["answer"] == full_answer
    assert "[... truncated ...]" not in response["answer"]


def test_external_agentic_adapter_reads_trace_final_content() -> None:
    store = make_store()
    full_answer = "trace final content " * 100

    class FakeFastreactClient:
        def chat_completion(self, **kwargs):
            return {
                "run_id": "run_trace_final",
                "trace": {
                    "final_content": full_answer,
                    "final_content_preview": "trace final content\n[... truncated ...]",
                    "final_content_truncated": True,
                },
            }

    service = FastreactAgenticServiceAdapter(
        AgenticServiceConfig(provider="fastreact", url="http://agentic.test"),
        client=FakeFastreactClient(),
    )

    response = service.search("读取 trace final", store.get_user("user_primary"))

    assert response["answer"] == full_answer.strip()
    assert "[... truncated ...]" not in response["answer"]


def test_external_agentic_adapter_extracts_fenced_json_payload() -> None:
    store = make_store()

    class FakeFastreactClient:
        def chat_completion(self, **kwargs):
            return {
                "run_id": "run_fenced",
                "content": (
                    "Here is the answer.\n"
                    "```json\n"
                    '{"answer":"你好啊！很高兴见到你。","retrieval":{"citations":[]},"trace":{"status":"ok"},"source_refs":[]}\n'
                    "```"
                ),
            }

    service = FastreactAgenticServiceAdapter(
        AgenticServiceConfig(provider="fastreact", url="http://agentic.test"),
        client=FakeFastreactClient(),
    )

    response = service.search("你好啊", store.get_user("user_primary"))

    assert response["answer"] == "你好啊！很高兴见到你。"
    assert response["trace"]["status"] == "ok"


def test_external_agentic_adapter_extracts_jsonish_background_answer() -> None:
    store = make_store()

    class FakeFastreactClient:
        def create_run(self, **kwargs):
            raise AssertionError("PSKA agentic adapter should use /v1/chat/completions, not /v1/runs")

        def chat_completion(self, **kwargs):
            return {
                "run_id": "chat_jsonish",
                "events": [
                    {
                        "type": "session_end",
                        "content": (
                            "Now I have all the information.\n"
                            "```json\n"
                            '{\n'
                            '  "answer": "大模型"0幻觉"要求包括边界约束、RAG证据链和人工兜底。",\n'
                            '  "retrieval": {"citations": []},\n'
                            '  "trace": {"status": "ok"},\n'
                            '  "source_refs": []\n'
                            "}\n"
                            "```"
                        ),
                    }
                ],
            }

    service = FastreactAgenticServiceAdapter(
        AgenticServiceConfig(provider="fastreact", url="http://agentic.test"),
        client=FakeFastreactClient(),
    )

    response = service.search("0幻觉", store.get_user("user_primary"))

    assert response["answer"] == '大模型"0幻觉"要求包括边界约束、RAG证据链和人工兜底。'
    assert "```json" not in response["answer"]
    assert response["agentic_service"]["run_id"] == "chat_jsonish"
