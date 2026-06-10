from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any

from pska_core.acl import ACLService
from pska_core.embeddings import EmbeddingProvider
from pska_core.models import Chunk, Entity, Hyperedge, HyperedgeMember, SourceItem, User
from pska_core.store import KnowledgeStore


@dataclass(slots=True)
class RetrievalResult:
    result_id: str
    source_item_id: str
    title: str
    snippet: str
    score: float
    citation: dict[str, Any]
    score_debug: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalResponse:
    query: str
    request_user_id: str
    visible_spaces: list[str]
    visible_team_ids: list[str]
    results: list[RetrievalResult]
    citations: list[dict[str, Any]]
    hypergraph_context: list[dict[str, Any]]
    profile_context_used: bool = False
    memory_context_used: bool = False
    gaps: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    score_debug: dict[str, Any] = field(default_factory=dict)


class RetrievalService:
    """Hybrid retrieval skeleton: ACL, lexical scoring, vector hook, graph expansion."""

    def __init__(self, store: KnowledgeStore, acl: ACLService, *, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.store = store
        self.acl = acl
        self.embedding_provider = embedding_provider

    def search(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        top_k: int = 5,
    ) -> RetrievalResponse:
        visible_items = self.acl.filter_visible_items(
            user,
            self.store.list_source_items(),
            represented_user_id=represented_user_id,
        )
        source_ids = {item.source_item_id for item in visible_items}
        chunks = self.store.list_chunks_for_sources(source_ids)
        ranked, rank_debug = self._rank(query, visible_items, chunks, source_ids, top_k=top_k)
        citations = [result.citation for result in ranked]
        visible_team_ids = sorted(self.acl.visible_team_ids_for_user(represented_user_id or user.user_id))
        hypergraph_context = self._hypergraph_context(
            query=query,
            ranked=ranked,
            user=user,
            represented_user_id=represented_user_id,
        )
        return RetrievalResponse(
            query=query,
            request_user_id=represented_user_id or user.user_id,
            visible_spaces=sorted({item.space_id for item in visible_items}),
            visible_team_ids=visible_team_ids,
            results=ranked,
            citations=citations,
            hypergraph_context=hypergraph_context,
            gaps=[] if ranked else ["insufficient_evidence"],
            score_debug={"ranker": "hybrid_rrf", "top_k": top_k, **rank_debug},
        )

    def _rank(
        self,
        query: str,
        items: list[SourceItem],
        chunks: list[Chunk],
        source_ids: set[str],
        *,
        top_k: int,
    ) -> tuple[list[RetrievalResult], dict[str, Any]]:
        item_by_id = {item.source_item_id: item for item in items}
        query_terms = self._terms(query)
        lexical_results: list[RetrievalResult] = []
        for chunk in chunks:
            item = item_by_id[chunk.source_item_id]
            text_terms = self._terms(f"{item.title} {chunk.text} {item.url or ''}")
            lexical = self._lexical_score(query_terms, text_terms)
            if lexical <= 0:
                continue
            lexical_results.append(self._result_for_chunk(chunk, item, lexical, {"lexical": lexical, "vector": 0.0}))
        lexical_ranked = sorted(lexical_results, key=lambda result: result.score, reverse=True)

        vector_ranked: list[RetrievalResult] = []
        vector_enabled = self.embedding_provider is not None
        if self.embedding_provider:
            query_embedding = self.embedding_provider.embed_texts([query])[0]
            for chunk, vector_score in self.store.vector_search_chunks(source_ids, query_embedding, top_k=max(top_k * 4, 20)):
                item = item_by_id.get(chunk.source_item_id)
                if not item:
                    continue
                vector_ranked.append(self._result_for_chunk(chunk, item, vector_score, {"lexical": 0.0, "vector": vector_score}))

        combined = self._rrf_merge(lexical_ranked, vector_ranked, top_k=top_k)
        return combined, {
            "lexical_candidates": len(lexical_ranked),
            "vector_enabled": vector_enabled,
            "vector_candidates": len(vector_ranked),
            "embedding_model": self.embedding_provider.model_name if self.embedding_provider else None,
        }

    def _result_for_chunk(self, chunk: Chunk, item: SourceItem, score: float, score_debug: dict[str, float]) -> RetrievalResult:
        return RetrievalResult(
            result_id=chunk.chunk_id,
            source_item_id=item.source_item_id,
            title=item.title,
            snippet=chunk.text[:240],
            score=score,
            citation={
                "source_item_id": item.source_item_id,
                "chunk_id": chunk.chunk_id,
                "url": item.url,
                "title": item.title,
            },
            score_debug=score_debug,
        )

    def _terms(self, text: str) -> list[str]:
        return re.findall(r"[\w\u4e00-\u9fff]+", text.lower())

    def _lexical_score(self, query_terms: list[str], text_terms: list[str]) -> float:
        if not query_terms or not text_terms:
            return 0.0
        counts = {term: text_terms.count(term) for term in set(query_terms)}
        return sum(counts.values()) / math.sqrt(len(text_terms))

    def _rrf_merge(
        self,
        lexical_ranked: list[RetrievalResult],
        vector_ranked: list[RetrievalResult],
        *,
        top_k: int,
        k: int = 60,
    ) -> list[RetrievalResult]:
        by_id: dict[str, RetrievalResult] = {}
        scores: dict[str, float] = {}
        for rank, result in enumerate(lexical_ranked, start=1):
            by_id.setdefault(result.result_id, result)
            scores[result.result_id] = scores.get(result.result_id, 0.0) + 1.0 / (k + rank)
            by_id[result.result_id].score_debug["lexical_rank"] = float(rank)
            by_id[result.result_id].score_debug["lexical"] = max(by_id[result.result_id].score_debug.get("lexical", 0.0), result.score_debug.get("lexical", 0.0))
        for rank, result in enumerate(vector_ranked, start=1):
            by_id.setdefault(result.result_id, result)
            scores[result.result_id] = scores.get(result.result_id, 0.0) + 1.0 / (k + rank)
            by_id[result.result_id].score_debug["vector_rank"] = float(rank)
            by_id[result.result_id].score_debug["vector"] = max(by_id[result.result_id].score_debug.get("vector", 0.0), result.score_debug.get("vector", 0.0))
        merged = list(by_id.values())
        for result in merged:
            result.score = scores.get(result.result_id, result.score)
        return sorted(merged, key=lambda result: result.score, reverse=True)[:top_k]

    def _hypergraph_context(
        self,
        *,
        query: str,
        ranked: list[RetrievalResult],
        user: User,
        represented_user_id: str | None,
    ) -> list[dict[str, Any]]:
        entities = self._matching_entities(query, ranked)
        visible_entities = [
            entity
            for entity in entities
            if self._can_read_graph_object(user, entity.owner_user_id, entity.visibility, entity.visible_team_ids, represented_user_id)
        ]
        edges = self.store.list_hyperedges_for_entities({entity.entity_id for entity in visible_entities})
        context = []
        entity_by_id = {entity.entity_id: entity for entity in self.store.list_entities()}
        for edge, members in edges:
            if not self._can_read_graph_object(user, edge.owner_user_id, edge.visibility, edge.visible_team_ids, represented_user_id):
                continue
            context.append(self._edge_context(edge, members, entity_by_id))
        return context

    def _matching_entities(self, query: str, ranked: list[RetrievalResult]) -> list[Entity]:
        haystack = " ".join([query, *[result.title for result in ranked], *[result.snippet for result in ranked]]).lower()
        matches = []
        for entity in self.store.list_entities():
            if entity.label.lower() in haystack:
                matches.append(entity)
        return matches

    def _can_read_graph_object(
        self,
        user: User,
        owner_user_id: str,
        visibility,
        visible_team_ids: list[str],
        represented_user_id: str | None,
    ) -> bool:
        if user.role == "admin":
            return True
        effective_user_id = represented_user_id if user.role == "agent_service" else user.user_id
        if owner_user_id == effective_user_id:
            return True
        if str(visibility) == "team":
            return bool(self.acl.visible_team_ids_for_user(effective_user_id).intersection(visible_team_ids))
        return str(visibility) == "public"

    def _edge_context(
        self,
        edge: Hyperedge,
        members: list[HyperedgeMember],
        entity_by_id: dict[str, Entity],
    ) -> dict[str, Any]:
        return {
            "hyperedge_id": edge.hyperedge_id,
            "relation_type": edge.relation_type,
            "directionality": str(edge.directionality),
            "evidence_text": edge.evidence_text,
            "confidence": edge.confidence,
            "members": [
                {
                    "entity_id": member.entity_id,
                    "role": member.role,
                    "label": entity_by_id.get(member.entity_id).label if entity_by_id.get(member.entity_id) else member.entity_id,
                    "entity_type": entity_by_id.get(member.entity_id).entity_type if entity_by_id.get(member.entity_id) else "unknown",
                }
                for member in members
            ],
        }
