from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import re
from typing import Any

from pska_core.acl import ACLService
from pska_core.embeddings import EmbeddingProvider
from pska_core.models import Chunk, Entity, Hyperedge, HyperedgeMember, SourceItem, SourceRef, User
from pska_core.serde import to_jsonable
from pska_core.store import KnowledgeStore


_CONFLICT_RELATION_TYPES = {"contradicts", "conflicts_with", "disputes", "refutes"}
_SENSITIVE_TERMS = {
    "api key",
    "bank",
    "diagnosis",
    "health",
    "medical",
    "passport",
    "password",
    "phone",
    "salary",
    "secret",
    "ssn",
    "tax",
    "地址",
    "护照",
    "密码",
    "密钥",
    "工资",
    "电话",
    "税",
    "身份证",
    "诊断",
    "银行",
    "医疗",
}


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
    graph_paths: list[dict[str, Any]] = field(default_factory=list)
    profile_context: list[dict[str, Any]] = field(default_factory=list)
    memory_context: list[dict[str, Any]] = field(default_factory=list)
    profile_context_used: bool = False
    memory_context_used: bool = False
    gaps: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    sensitivity: list[str] = field(default_factory=list)
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
        graph_paths = self._graph_paths(
            query=query,
            ranked=ranked,
            user=user,
            represented_user_id=represented_user_id,
        )
        profile_context = self._profile_context(query=query, user=user, represented_user_id=represented_user_id)
        memory_context = self._memory_context(query=query, user=user, represented_user_id=represented_user_id)
        ranker = "exact_source" if rank_debug.get("exact_candidates", 0) else "hybrid_rrf"
        graph_context_used = bool(hypergraph_context or graph_paths)
        diagnostics = self._diagnostics(
            query=query,
            ranked=ranked,
            visible_items=visible_items,
            hypergraph_context=hypergraph_context,
            graph_paths=graph_paths,
            profile_context=profile_context,
            memory_context=memory_context,
        )
        return RetrievalResponse(
            query=query,
            request_user_id=represented_user_id or user.user_id,
            visible_spaces=sorted({item.space_id for item in visible_items}),
            visible_team_ids=visible_team_ids,
            results=ranked,
            citations=citations,
            hypergraph_context=hypergraph_context,
            graph_paths=graph_paths,
            profile_context=profile_context,
            memory_context=memory_context,
            profile_context_used=bool(profile_context),
            memory_context_used=bool(memory_context),
            gaps=diagnostics["gaps"],
            conflicts=diagnostics["conflicts"],
            sensitivity=diagnostics["sensitivity"],
            score_debug={
                "ranker": ranker,
                "top_k": top_k,
                "graph_context_used": graph_context_used,
                "graph_paths_used": bool(graph_paths),
                "diagnostics": diagnostics["score_debug"],
                **rank_debug,
            },
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
        exact_ranked = self._exact_source_results(query, items, chunks, item_by_id, top_k=top_k)
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

        combined = self._merge_exact_then_rrf(exact_ranked, lexical_ranked, vector_ranked, top_k=top_k)
        self._apply_rank_quality_boosts(combined, item_by_id, reference_time=_latest_source_time(items))
        return combined, {
            "exact_candidates": len(exact_ranked),
            "lexical_candidates": len(lexical_ranked),
            "vector_enabled": vector_enabled,
            "vector_candidates": len(vector_ranked),
            "embedding_model": self.embedding_provider.model_name if self.embedding_provider else None,
        }

    def _exact_source_results(
        self,
        query: str,
        items: list[SourceItem],
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
        *,
        top_k: int,
    ) -> list[RetrievalResult]:
        normalized_query = _normalize_exact(query)
        if not normalized_query:
            return []

        exact_source_ids = {
            item.source_item_id
            for item in items
            if normalized_query in {
                _normalize_exact(item.source_item_id),
                _normalize_exact(item.source_id),
                _normalize_exact(item.title),
                _normalize_exact(item.url or ""),
            }
        }
        exact_results = [
            self._result_for_chunk(
                chunk,
                item_by_id[chunk.source_item_id],
                1.0,
                {"exact_source": 1.0, "lexical": 0.0, "vector": 0.0},
            )
            for chunk in chunks
            if chunk.source_item_id in exact_source_ids
        ]
        return exact_results[:top_k]

    def _result_for_chunk(self, chunk: Chunk, item: SourceItem, score: float, score_debug: dict[str, float]) -> RetrievalResult:
        citation = {
            "source_item_id": item.source_item_id,
            "chunk_id": chunk.chunk_id,
            "url": item.url,
            "title": item.title,
        }
        message_ids = _conversation_message_ids(item, chunk.text)
        if message_ids:
            citation["message_ids"] = message_ids
        return RetrievalResult(
            result_id=chunk.chunk_id,
            source_item_id=item.source_item_id,
            title=item.title,
            snippet=chunk.text[:240],
            score=score,
            citation=citation,
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

    def _merge_exact_then_rrf(
        self,
        exact_ranked: list[RetrievalResult],
        lexical_ranked: list[RetrievalResult],
        vector_ranked: list[RetrievalResult],
        *,
        top_k: int,
    ) -> list[RetrievalResult]:
        if not exact_ranked:
            return self._rrf_merge(lexical_ranked, vector_ranked, top_k=top_k)

        exact_ids = {result.result_id for result in exact_ranked}
        remainder = self._rrf_merge(
            [result for result in lexical_ranked if result.result_id not in exact_ids],
            [result for result in vector_ranked if result.result_id not in exact_ids],
            top_k=max(top_k - len(exact_ranked), 0),
        )
        return [*exact_ranked, *remainder][:top_k]

    def _apply_rank_quality_boosts(
        self,
        results: list[RetrievalResult],
        item_by_id: dict[str, SourceItem],
        *,
        reference_time: datetime | None,
    ) -> None:
        for result in results:
            item = item_by_id[result.source_item_id]
            recency = _source_recency_score(item, reference_time=reference_time)
            authority = _source_authority_score(item)
            base_score = result.score
            boost = recency * 0.004 + authority * 0.004
            result.score = base_score + boost
            result.score_debug["base_score"] = base_score
            result.score_debug["recency"] = recency
            result.score_debug["source_authority"] = authority
            result.score_debug["quality_boost"] = boost
        results.sort(key=lambda result: result.score, reverse=True)

    def _diagnostics(
        self,
        *,
        query: str,
        ranked: list[RetrievalResult],
        visible_items: list[SourceItem],
        hypergraph_context: list[dict[str, Any]],
        graph_paths: list[dict[str, Any]],
        profile_context: list[dict[str, Any]],
        memory_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        item_by_id = {item.source_item_id: item for item in visible_items}
        gaps: list[str] = []
        conflicts: list[str] = []
        sensitivity: list[str] = []

        if not ranked and not hypergraph_context and not graph_paths:
            gaps.append("insufficient_evidence")

        graph_edges = _dedupe_edge_contexts([*hypergraph_context, *[edge for path in graph_paths for edge in path.get("edges", [])]])
        ungrounded_edges = [edge for edge in graph_edges if not edge.get("evidence_citations")]
        if graph_edges and ungrounded_edges:
            gaps.append("ungrounded_graph_context")

        for edge in graph_edges:
            relation_type = str(edge.get("relation_type") or "").lower()
            if relation_type in _CONFLICT_RELATION_TYPES:
                conflicts.append(f"graph_conflict:{edge.get('hyperedge_id')}:{relation_type}")

        sensitive_query_terms = _sensitive_terms_in_text(query)
        if sensitive_query_terms:
            sensitivity.append(f"sensitive_query_terms:{','.join(sensitive_query_terms)}")

        sensitive_source_ids = [
            result.source_item_id
            for result in ranked
            if _source_sensitivity(item_by_id.get(result.source_item_id)) in {"high", "sensitive"}
        ]
        if sensitive_source_ids:
            sensitivity.append(f"sensitive_sources:{','.join(sorted(set(sensitive_source_ids)))}")

        sensitive_memory_ids = [
            str(item["agent_memory_id"])
            for item in memory_context
            if _sensitive_terms_in_text(str(item.get("text") or ""))
        ]
        if sensitive_memory_ids:
            sensitivity.append(f"sensitive_memory_context:{','.join(sorted(set(sensitive_memory_ids)))}")

        sensitive_profile_ids = [
            str(item["profile_card_id"])
            for item in profile_context
            if _sensitive_terms_in_text(json.dumps(item.get("profile") or {}, ensure_ascii=False))
        ]
        if sensitive_profile_ids:
            sensitivity.append(f"sensitive_profile_context:{','.join(sorted(set(sensitive_profile_ids)))}")

        return {
            "gaps": gaps,
            "conflicts": conflicts,
            "sensitivity": sensitivity,
            "score_debug": {
                "ungrounded_graph_edges": len(ungrounded_edges),
                "conflict_count": len(conflicts),
                "sensitivity_count": len(sensitivity),
                "profile_context_count": len(profile_context),
                "memory_context_count": len(memory_context),
            },
        }

    def _profile_context(
        self,
        *,
        query: str,
        user: User,
        represented_user_id: str | None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        owner_user_id = _context_owner_user_id(user, represented_user_id)
        query_terms = self._terms(query)
        contexts = []
        for card in self.store.list_profile_cards(owner_user_id=owner_user_id):
            text = json.dumps(card.profile, ensure_ascii=False, sort_keys=True)
            lexical = self._lexical_score(query_terms, self._terms(text))
            if lexical <= 0 and not _is_profile_query(query):
                continue
            citations = self._source_ref_citations(card.source_refs, user=user, represented_user_id=represented_user_id)
            contexts.append(
                {
                    "profile_card_id": card.profile_card_id,
                    "profile": card.profile,
                    "confidence": card.confidence,
                    "source_refs": to_jsonable(card.source_refs),
                    "citations": citations,
                    "score": card.confidence * 0.7 + lexical * 0.3,
                    "score_debug": {
                        "lexical": lexical,
                        "confidence": card.confidence,
                        "has_citations": bool(citations),
                    },
                }
            )
        return sorted(contexts, key=lambda item: (item["score"], item["profile_card_id"]), reverse=True)[:limit]

    def _memory_context(
        self,
        *,
        query: str,
        user: User,
        represented_user_id: str | None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        owner_user_id = _context_owner_user_id(user, represented_user_id)
        query_terms = self._terms(query)
        contexts = []
        for memory in self.store.list_agent_memories(owner_user_id=owner_user_id):
            if memory.confidence <= 0 or memory.decay_policy == "forgotten":
                continue
            lexical = self._lexical_score(query_terms, self._terms(memory.text))
            if lexical <= 0 and not _is_memory_query(query):
                continue
            citations = self._source_ref_citations(memory.source_refs, user=user, represented_user_id=represented_user_id)
            contexts.append(
                {
                    "agent_memory_id": memory.agent_memory_id,
                    "layer": str(memory.layer),
                    "text": memory.text,
                    "confidence": memory.confidence,
                    "decay_policy": memory.decay_policy,
                    "last_verified_at": memory.last_verified_at.isoformat() if memory.last_verified_at else None,
                    "created_by_user_id": memory.created_by_user_id,
                    "source_refs": to_jsonable(memory.source_refs),
                    "citations": citations,
                    "score": memory.confidence * 0.7 + lexical * 0.3,
                    "score_debug": {
                        "lexical": lexical,
                        "confidence": memory.confidence,
                        "has_citations": bool(citations),
                    },
                }
            )
        return sorted(contexts, key=lambda item: (item["score"], item["agent_memory_id"]), reverse=True)[:limit]

    def _hypergraph_context(
        self,
        *,
        query: str,
        ranked: list[RetrievalResult],
        user: User,
        represented_user_id: str | None,
    ) -> list[dict[str, Any]]:
        entities = self._matching_entities(query, ranked)
        if _is_graph_global_query(query):
            entities = self.store.list_entities()
        visible_entities = self._visible_entities(entities, user=user, represented_user_id=represented_user_id)
        edges = self.store.list_hyperedges_for_entities({entity.entity_id for entity in visible_entities})
        context = []
        entity_by_id = {entity.entity_id: entity for entity in self.store.list_entities()}
        for edge, members in sorted(edges, key=lambda item: item[0].hyperedge_id):
            if not self._can_read_graph_object(user, edge.owner_user_id, edge.visibility, edge.visible_team_ids, represented_user_id):
                continue
            context.append(self._edge_context(edge, members, entity_by_id, user=user, represented_user_id=represented_user_id))
        return context

    def _graph_paths(
        self,
        *,
        query: str,
        ranked: list[RetrievalResult],
        user: User,
        represented_user_id: str | None,
        max_depth: int = 2,
        max_paths: int = 8,
    ) -> list[dict[str, Any]]:
        normalized_query = _normalize_match_text(query)
        seed_entities = self._visible_entities(
            self._matching_entities(query, ranked),
            user=user,
            represented_user_id=represented_user_id,
        )
        if not seed_entities:
            return []

        all_entities = self.store.list_entities()
        entity_by_id = {entity.entity_id: entity for entity in all_entities}
        visible_entity_ids = {
            entity.entity_id
            for entity in self._visible_entities(all_entities, user=user, represented_user_id=represented_user_id)
        }
        paths: list[dict[str, Any]] = []
        seen_paths: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
        candidate_limit = max_paths * 4

        for seed in seed_entities:
            frontier: list[tuple[str, list[str], list[dict[str, Any]], list[str]]] = [
                (seed.entity_id, [seed.entity_id], [], [])
            ]
            for _depth in range(max_depth):
                next_frontier: list[tuple[str, list[str], list[dict[str, Any]], list[str]]] = []
                for current_entity_id, entity_ids, edge_contexts, edge_ids in frontier:
                    edges = sorted(
                        self.store.list_hyperedges_for_entities({current_entity_id}),
                        key=lambda item: item[0].hyperedge_id,
                    )
                    for edge, members in edges:
                        if edge.hyperedge_id in edge_ids:
                            continue
                        if not self._can_read_graph_object(user, edge.owner_user_id, edge.visibility, edge.visible_team_ids, represented_user_id):
                            continue
                        visible_member_ids = [member.entity_id for member in members if member.entity_id in visible_entity_ids]
                        if current_entity_id not in visible_member_ids or len(visible_member_ids) < 2:
                            continue

                        edge_context = self._edge_context(edge, members, entity_by_id, user=user, represented_user_id=represented_user_id)
                        for neighbor_entity_id in visible_member_ids:
                            if neighbor_entity_id == current_entity_id:
                                continue
                            if neighbor_entity_id in entity_ids:
                                continue
                            next_entity_ids = [*entity_ids, neighbor_entity_id]
                            next_edge_ids = [*edge_ids, edge.hyperedge_id]
                            path_key = (seed.entity_id, tuple(next_edge_ids), tuple(next_entity_ids))
                            if path_key in seen_paths:
                                continue
                            seen_paths.add(path_key)

                            next_edge_contexts = [*edge_contexts, edge_context]
                            path_entities = [
                                self._entity_context(entity_by_id[entity_id])
                                for entity_id in next_entity_ids
                                if entity_id in entity_by_id
                            ]
                            score, score_debug = _graph_path_score(
                                path_entities,
                                next_edge_contexts,
                                normalized_query=normalized_query,
                            )
                            paths.append(
                                {
                                    "path_id": f"{seed.entity_id}:{'|'.join(next_edge_ids)}:{neighbor_entity_id}",
                                    "depth": len(next_edge_ids),
                                    "seed": self._entity_context(seed),
                                    "entities": path_entities,
                                    "edges": next_edge_contexts,
                                    "explanation": _graph_path_explanation(path_entities, next_edge_contexts),
                                    "score": score,
                                    "score_debug": score_debug,
                                }
                            )
                            if len(paths) >= candidate_limit:
                                return _rank_graph_paths(paths, max_paths=max_paths)
                            if len(next_edge_ids) < max_depth:
                                next_frontier.append((neighbor_entity_id, next_entity_ids, next_edge_contexts, next_edge_ids))
                frontier = next_frontier
                if not frontier:
                    break
        return _rank_graph_paths(paths, max_paths=max_paths)

    def _matching_entities(self, query: str, ranked: list[RetrievalResult]) -> list[Entity]:
        haystack = " ".join([query, *[result.title for result in ranked], *[result.snippet for result in ranked]])
        normalized_haystack = _normalize_match_text(haystack)
        matches: list[tuple[int, int, str, Entity]] = []
        for entity in self.store.list_entities():
            for alias in _entity_aliases(entity):
                normalized_alias = _normalize_match_text(alias)
                if not normalized_alias:
                    continue
                position = _match_position(normalized_haystack, normalized_alias)
                if position is None:
                    continue
                matches.append((position, -len(normalized_alias), entity.entity_id, entity))
                break
        return [item[3] for item in sorted(matches)]

    def _visible_entities(
        self,
        entities: list[Entity],
        *,
        user: User,
        represented_user_id: str | None,
    ) -> list[Entity]:
        return [
            entity
            for entity in entities
            if self._can_read_graph_object(user, entity.owner_user_id, entity.visibility, entity.visible_team_ids, represented_user_id)
        ]

    def _entity_context(self, entity: Entity) -> dict[str, Any]:
        return {
            "entity_id": entity.entity_id,
            "label": entity.label,
            "entity_type": entity.entity_type,
        }

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
        *,
        user: User,
        represented_user_id: str | None,
    ) -> dict[str, Any]:
        evidence_citations = self._edge_evidence_citations(edge, user=user, represented_user_id=represented_user_id)
        visible_source_refs = self._visible_edge_source_refs(edge, user=user, represented_user_id=represented_user_id)
        return {
            "hyperedge_id": edge.hyperedge_id,
            "relation_type": edge.relation_type,
            "directionality": str(edge.directionality),
            "evidence_text": edge.evidence_text,
            "confidence": edge.confidence,
            "source_refs": to_jsonable(visible_source_refs),
            "evidence_citations": evidence_citations,
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

    def _visible_edge_source_refs(
        self,
        edge: Hyperedge,
        *,
        user: User,
        represented_user_id: str | None,
    ) -> list[Any]:
        source_item_ids = {ref.source_item_id for ref in edge.source_refs if ref.source_item_id}
        if not source_item_ids:
            return []
        visible_ids = {
            item.source_item_id
            for item in self.store.list_source_items()
            if item.source_item_id in source_item_ids
            and self.acl.can_read_item(user, item, represented_user_id=represented_user_id)
        }
        return [ref for ref in edge.source_refs if ref.source_item_id in visible_ids]

    def _edge_evidence_citations(
        self,
        edge: Hyperedge,
        *,
        user: User,
        represented_user_id: str | None,
    ) -> list[dict[str, Any]]:
        return self._source_ref_citations(edge.source_refs, user=user, represented_user_id=represented_user_id)

    def _source_ref_citations(
        self,
        source_refs: list[SourceRef],
        *,
        user: User,
        represented_user_id: str | None,
    ) -> list[dict[str, Any]]:
        source_item_ids = {ref.source_item_id for ref in source_refs if ref.source_item_id}
        if not source_item_ids:
            return []
        items = [
            item
            for item in self.store.list_source_items()
            if item.source_item_id in source_item_ids
            and self.acl.can_read_item(user, item, represented_user_id=represented_user_id)
        ]
        chunks = self.store.list_chunks_for_sources({item.source_item_id for item in items})
        chunks_by_source: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            chunks_by_source.setdefault(chunk.source_item_id, []).append(chunk)

        citations = []
        for item in sorted(items, key=lambda current: current.source_item_id):
            item_chunks = sorted(chunks_by_source.get(item.source_item_id, []), key=lambda chunk: chunk.ordinal)
            if not item_chunks:
                citations.append(
                    {
                        "source_item_id": item.source_item_id,
                        "url": item.url,
                        "title": item.title,
                    }
                )
                continue
            for chunk in item_chunks[:2]:
                citations.append(
                    {
                        "source_item_id": item.source_item_id,
                        "chunk_id": chunk.chunk_id,
                        "url": item.url,
                        "title": item.title,
                        "snippet": chunk.text[:240],
                    }
                )
        return citations


def _conversation_message_ids(item: SourceItem, text: str) -> list[str]:
    if item.record_type != "conversation":
        return []
    content = item.metadata.get("content") or {}
    messages = content.get("messages") if isinstance(content, dict) else []
    if not isinstance(messages, list):
        return []
    message_ids: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_id = message.get("message_id")
        if message_id and str(message_id) in text:
            message_ids.append(str(message_id))
    return message_ids


def _context_owner_user_id(user: User, represented_user_id: str | None) -> str:
    if user.role == "agent_service" and represented_user_id:
        return represented_user_id
    return represented_user_id or user.user_id


def _normalize_exact(value: str) -> str:
    return value.strip().lower()


def _latest_source_time(items: list[SourceItem]) -> datetime | None:
    timestamps = [_source_time(item) for item in items]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(timestamps) if timestamps else None


def _source_time(item: SourceItem) -> datetime | None:
    for value in (item.metadata.get("created_at"), item.metadata.get("captured_at"), item.created_at):
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_recency_score(item: SourceItem, *, reference_time: datetime | None) -> float:
    source_time = _source_time(item)
    if source_time is None or reference_time is None:
        return 0.0
    age_days = max((reference_time - source_time).total_seconds() / 86400, 0.0)
    half_life_days = 180.0
    return round(0.5 ** (age_days / half_life_days), 6)


def _source_authority_score(item: SourceItem) -> float:
    metadata = item.metadata or {}
    extra = metadata.get("extra") if isinstance(metadata.get("extra"), dict) else {}
    candidates = [
        metadata.get("source_authority"),
        metadata.get("authority"),
        extra.get("source_authority"),
        extra.get("authority"),
    ]
    for candidate in candidates:
        score = _coerce_unit_float(candidate)
        if score is not None:
            return score

    channel_defaults = {
        "manual": 0.65,
        "conversation": 0.6,
        "browser": 0.55,
        "web": 0.55,
        "git": 0.7,
        "twitter": 0.5,
        "x": 0.5,
    }
    return channel_defaults.get(item.source_channel.lower(), 0.5)


def _coerce_unit_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, numeric))


def _dedupe_edge_contexts(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for edge in edges:
        edge_id = str(edge.get("hyperedge_id") or "")
        if not edge_id:
            continue
        if edge_id in seen:
            continue
        seen.add(edge_id)
        deduped.append(edge)
    return deduped


def _sensitive_terms_in_text(text: str) -> list[str]:
    normalized = _normalize_match_text(text)
    matches = []
    for term in sorted(_SENSITIVE_TERMS):
        if _match_position(normalized, _normalize_match_text(term)) is not None:
            matches.append(term)
    return matches


def _source_sensitivity(item: SourceItem | None) -> str:
    if item is None:
        return "normal"
    metadata = item.metadata or {}
    extra = metadata.get("extra") if isinstance(metadata.get("extra"), dict) else {}
    for value in (metadata.get("sensitivity"), metadata.get("classification"), extra.get("sensitivity"), extra.get("classification")):
        if not value:
            continue
        normalized = str(value).strip().lower()
        if normalized in {"high", "sensitive"}:
            return normalized
    return "normal"


def _entity_aliases(entity: Entity) -> list[str]:
    aliases: list[str] = [entity.label]
    metadata_aliases = entity.metadata.get("aliases") or entity.metadata.get("alias") or []
    if isinstance(metadata_aliases, str):
        metadata_aliases = [metadata_aliases]
    if isinstance(metadata_aliases, list):
        aliases.extend(str(alias) for alias in metadata_aliases if str(alias).strip())
    for metadata_key in ("canonical_label", "slug", "handle"):
        value = entity.metadata.get(metadata_key)
        if value:
            aliases.append(str(value))

    seen: set[str] = set()
    normalized_aliases = []
    for alias in aliases:
        normalized = alias.strip()
        if not normalized:
            continue
        dedupe_key = _normalize_match_text(normalized)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized_aliases.append(normalized)
    return normalized_aliases


def _normalize_match_text(value: str) -> str:
    normalized = re.sub(r"[_\-/]+", " ", value.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _match_position(haystack: str, alias: str) -> int | None:
    if _contains_cjk(alias):
        position = haystack.find(alias)
        return position if position >= 0 else None

    pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
    match = re.search(pattern, haystack)
    if match:
        return match.start()

    collapsed_alias = alias.replace(" ", "")
    if collapsed_alias != alias and len(collapsed_alias) >= 3:
        collapsed_haystack = haystack.replace(" ", "")
        position = collapsed_haystack.find(collapsed_alias)
        return position if position >= 0 else None
    return None


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _is_profile_query(query: str) -> bool:
    normalized = query.lower()
    terms = set(re.findall(r"[\w\u4e00-\u9fff]+", normalized))
    return bool(terms.intersection({"profile", "preference", "preferences", "style", "about", "me"})) or any(
        term in normalized for term in ("画像", "偏好", "关于我", "个人资料")
    )


def _is_memory_query(query: str) -> bool:
    normalized = query.lower()
    terms = set(re.findall(r"[\w\u4e00-\u9fff]+", normalized))
    return bool(terms.intersection({"memory", "remember", "preference", "preferences", "context"})) or any(
        term in normalized for term in ("记忆", "偏好", "上下文")
    )


def _graph_path_score(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    normalized_query: str,
) -> tuple[float, dict[str, Any]]:
    mean_confidence = _mean([float(edge.get("confidence") or 0.0) for edge in edges])
    evidence_coverage = _mean([1.0 if edge.get("evidence_citations") else 0.0 for edge in edges])
    mentioned_entities = [
        entity
        for entity in entities
        if _match_position(normalized_query, _normalize_match_text(str(entity.get("label", "")))) is not None
    ]
    mention_coverage = len(mentioned_entities) / len(entities) if entities else 0.0
    path_length = len(edges)
    length_penalty = 0.08 * max(path_length - 1, 0)
    score = mean_confidence * 0.45 + evidence_coverage * 0.35 + mention_coverage * 0.25 - length_penalty
    return round(score, 6), {
        "path_length": path_length,
        "mean_confidence": mean_confidence,
        "evidence_coverage": evidence_coverage,
        "query_mention_coverage": mention_coverage,
        "query_mentioned_entity_ids": [str(entity["entity_id"]) for entity in mentioned_entities],
        "length_penalty": length_penalty,
    }


def _rank_graph_paths(paths: list[dict[str, Any]], *, max_paths: int) -> list[dict[str, Any]]:
    return sorted(
        paths,
        key=lambda path: (
            float(path.get("score") or 0.0),
            -int(path.get("depth") or 0),
            str(path.get("path_id") or ""),
        ),
        reverse=True,
    )[:max_paths]


def _graph_path_explanation(entities: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    if not entities:
        return ""
    parts = [str(entities[0].get("label") or entities[0].get("entity_id") or "unknown")]
    for index, edge in enumerate(edges, start=1):
        relation = str(edge.get("relation_type") or "related_to")
        next_entity = entities[index] if index < len(entities) else {}
        next_label = str(next_entity.get("label") or next_entity.get("entity_id") or "unknown")
        parts.append(f"-[{relation}]-> {next_label}")
    return " ".join(parts)


def _is_graph_global_query(query: str) -> bool:
    normalized = query.lower()
    terms = set(re.findall(r"[\w\u4e00-\u9fff]+", normalized))
    graph_terms = {"graph", "knowledge", "entity", "entities", "relation", "relations", "relationship", "relationships"}
    graph_terms_zh = ("图谱", "实体", "关系", "关联")
    return bool(terms.intersection(graph_terms)) or any(term in normalized for term in graph_terms_zh)


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
