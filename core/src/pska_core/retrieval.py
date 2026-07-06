from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
import re
from typing import Any

from pska_core.acl import ACLService
from pska_core.embeddings import EmbeddingProvider
from pska_core.hipporag_index import HippoRAGOfflineIndex
from pska_core.models import Chunk, Entity, Hyperedge, HyperedgeMember, KnowledgeClaim, SourceItem, SourceRef, User
from pska_core.offline_index import OfflineIndexService
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
_RETRIEVAL_MODE_ALIASES = {
    "bm25": "lexical",
    "embedding": "vector",
    "keyword": "lexical",
    "semantic": "vector",
}


def _default_graph_embedding_linking(embedding_provider: EmbeddingProvider | None) -> bool:
    configured = _env_bool("PSKA_RETRIEVAL_GRAPH_EMBEDDING_LINKING")
    if configured is not None:
        return configured
    if embedding_provider is None:
        return False
    provider_name = str(getattr(embedding_provider, "provider_name", "")).strip().lower()
    model_name = str(getattr(embedding_provider, "model_name", "")).strip().lower()
    return provider_name not in {"bge-m3", "bge_m3", "bge"} and model_name not in {"baai/bge-m3", "bge-m3"}


def _env_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _normalize_retrieval_mode(value: str | None) -> str:
    normalized = str(value or "hybrid").strip().lower().replace("-", "_")
    normalized = _RETRIEVAL_MODE_ALIASES.get(normalized, normalized)
    if normalized in {"hybrid", "lexical", "vector"}:
        return normalized
    return "hybrid"


@dataclass(slots=True)
class RetrievalResult:
    result_id: str
    source_item_id: str
    source: str
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


@dataclass(frozen=True, slots=True)
class EvidenceScoreContext:
    query: str
    anchors: tuple[str, ...]
    asks_for_numeric_answer: bool


@dataclass(frozen=True, slots=True)
class EvidenceScoreFeatures:
    snippet: str
    text: str
    anchor_coverage: float
    has_numeric_evidence: bool
    looks_tabular: bool


@dataclass(frozen=True, slots=True)
class EvidenceScoreSignal:
    name: str
    score: float


@dataclass(frozen=True, slots=True)
class EvidenceScoreOutcome:
    score: float
    positive_score: float
    debug: dict[str, float]


class EvidenceScorer:
    name = "base"

    def score(
        self,
        result: RetrievalResult,
        context: EvidenceScoreContext,
        features: EvidenceScoreFeatures,
    ) -> EvidenceScoreSignal | None:
        raise NotImplementedError


class AnchorCoverageScorer(EvidenceScorer):
    name = "anchor_coverage_score"

    def score(
        self,
        result: RetrievalResult,
        context: EvidenceScoreContext,
        features: EvidenceScoreFeatures,
    ) -> EvidenceScoreSignal | None:
        if features.anchor_coverage <= 0:
            return None
        score = min(features.anchor_coverage * 0.018, 0.018)
        if features.anchor_coverage >= 0.75:
            score += 0.018
        return EvidenceScoreSignal(self.name, score)


class NumericEvidenceScorer(EvidenceScorer):
    name = "numeric_evidence_score"

    def score(
        self,
        result: RetrievalResult,
        context: EvidenceScoreContext,
        features: EvidenceScoreFeatures,
    ) -> EvidenceScoreSignal | None:
        if not context.asks_for_numeric_answer:
            return None
        if features.anchor_coverage < 0.45 or not features.has_numeric_evidence:
            return None
        return EvidenceScoreSignal(self.name, 0.018)


class TableEvidenceScorer(EvidenceScorer):
    name = "table_evidence_score"

    def score(
        self,
        result: RetrievalResult,
        context: EvidenceScoreContext,
        features: EvidenceScoreFeatures,
    ) -> EvidenceScoreSignal | None:
        if features.anchor_coverage < 0.45 or not features.looks_tabular:
            return None
        return EvidenceScoreSignal(self.name, 0.012)


class ValidationTablePenaltyScorer(EvidenceScorer):
    name = "validation_table_penalty"

    def score(
        self,
        result: RetrievalResult,
        context: EvidenceScoreContext,
        features: EvidenceScoreFeatures,
    ) -> EvidenceScoreSignal | None:
        penalty = _validation_table_penalty(features.snippet, context.query)
        if not penalty:
            return None
        return EvidenceScoreSignal(self.name, -penalty)


class EvidenceScorePipeline:
    name = "deterministic_evidence_scoring"

    def __init__(self, scorers: list[EvidenceScorer] | None = None, *, positive_score_cap: float = 0.055) -> None:
        self.scorers = scorers or [
            AnchorCoverageScorer(),
            NumericEvidenceScorer(),
            TableEvidenceScorer(),
            ValidationTablePenaltyScorer(),
        ]
        self.positive_score_cap = positive_score_cap

    def score(self, result: RetrievalResult, context: EvidenceScoreContext) -> EvidenceScoreOutcome:
        features = self._features(result, context)
        debug: dict[str, float] = {}
        if features.anchor_coverage:
            debug["evidence_scoring_anchor_coverage"] = features.anchor_coverage
            debug["stage2_anchor_coverage"] = features.anchor_coverage

        positive_score = 0.0
        negative_score = 0.0
        for scorer in self.scorers:
            signal = scorer.score(result, context, features)
            if signal is None or signal.score == 0:
                continue
            debug[f"evidence_scoring_{signal.name}"] = signal.score
            if signal.score > 0:
                positive_score += signal.score
            else:
                negative_score += signal.score
                if signal.name == "validation_table_penalty":
                    debug["validation_table_penalty"] = abs(signal.score)

        positive_score = min(positive_score, self.positive_score_cap)
        score = positive_score + negative_score
        if positive_score:
            debug["evidence_scoring_positive_score"] = positive_score
            debug["stage2_evidence_score"] = positive_score
        if score:
            debug["evidence_scoring_score"] = score
        return EvidenceScoreOutcome(score=score, positive_score=positive_score, debug=debug)

    def _features(self, result: RetrievalResult, context: EvidenceScoreContext) -> EvidenceScoreFeatures:
        text = f"{result.title}\n{result.snippet}"
        anchor_coverage = _anchor_coverage(list(context.anchors), text)
        return EvidenceScoreFeatures(
            snippet=result.snippet,
            text=text,
            anchor_coverage=anchor_coverage,
            has_numeric_evidence=_text_has_numeric_evidence(result.snippet),
            looks_tabular=_looks_like_tabular_evidence(result.snippet),
        )


class RetrievalService:
    """Hybrid retrieval: ACL, lexical/vector RRF, and request-scoped graph-aware expansion."""

    def __init__(
        self,
        store: KnowledgeStore,
        acl: ACLService,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        graph_embedding_linking: bool | None = None,
    ) -> None:
        self.store = store
        self.acl = acl
        self.embedding_provider = embedding_provider
        self.evidence_score_pipeline = EvidenceScorePipeline()
        self.graph_embedding_linking = (
            _default_graph_embedding_linking(embedding_provider)
            if graph_embedding_linking is None
            else graph_embedding_linking
        )

    def search(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        top_k: int = 5,
        source_item_ids: set[str] | None = None,
        scope_mode: str = "soft",
        retrieval_mode: str = "hybrid",
    ) -> RetrievalResponse:
        visible_items = self.acl.filter_visible_items(
            user,
            [item for item in self.store.list_source_items(tenant_id=user.tenant_id) if _is_active_source_item(item)],
            represented_user_id=represented_user_id,
        )
        source_ids = {item.source_item_id for item in visible_items}
        scoped_source_item_ids = set(source_item_ids or set()) & source_ids
        hard_scope = scope_mode == "hard" and source_item_ids is not None
        if hard_scope:
            visible_items = [item for item in visible_items if item.source_item_id in scoped_source_item_ids]
            source_ids = {item.source_item_id for item in visible_items}
        chunks = self.store.list_chunks_for_sources(source_ids)
        ranked, rank_debug = self._rank(
            query,
            visible_items,
            chunks,
            source_ids,
            scoped_source_item_ids=scoped_source_item_ids,
            user=user,
            represented_user_id=represented_user_id,
            top_k=top_k,
            retrieval_mode=retrieval_mode,
        )
        citations = [result.citation for result in ranked]
        visible_team_ids = sorted(self.acl.visible_team_ids_for_user(represented_user_id or user.user_id, tenant_id=user.tenant_id))
        if hard_scope:
            hypergraph_context: list[dict[str, Any]] = []
            graph_paths: list[dict[str, Any]] = []
            profile_context: list[dict[str, Any]] = []
            memory_context: list[dict[str, Any]] = []
        else:
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
        ranker = (
            "scoped_source"
            if rank_debug.get("scoped_candidates", 0)
            else "exact_source"
            if rank_debug.get("exact_candidates", 0)
            else "hybrid_rrf"
        )
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
        offline_index_freshness = OfflineIndexService(
            self.store,
            embedding_provider=self.embedding_provider,
        ).freshness(owner_user_id=represented_user_id or user.user_id, tenant_id=user.tenant_id)
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
                "offline_index_freshness": offline_index_freshness,
                "scope_mode": "hard" if hard_scope else "soft",
                "scope_source_items": len(scoped_source_item_ids),
                "scope_leak_prevention": hard_scope,
                "retrieval_mode": _normalize_retrieval_mode(retrieval_mode),
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
        scoped_source_item_ids: set[str],
        user: User,
        represented_user_id: str | None,
        top_k: int,
        retrieval_mode: str,
    ) -> tuple[list[RetrievalResult], dict[str, Any]]:
        normalized_mode = _normalize_retrieval_mode(retrieval_mode)
        lexical_enabled = normalized_mode in {"hybrid", "lexical"}
        vector_requested = normalized_mode in {"hybrid", "vector"}
        item_by_id = {item.source_item_id: item for item in items}
        query_terms = self._terms(query)
        rank_pool_size = max(top_k, min(len(chunks) or top_k, max(top_k * 4, top_k + 8)))
        scoped_ranked = (
            self._scoped_source_results(scoped_source_item_ids, chunks, item_by_id, query_terms=query_terms, top_k=rank_pool_size)
            if normalized_mode in {"hybrid", "lexical"} and len(scoped_source_item_ids) == 1
            else []
        )
        exact_ranked = (
            self._exact_source_results(query, items, chunks, item_by_id, top_k=rank_pool_size)
            if lexical_enabled
            else []
        )
        exact_identifier_ranked = (
            self._exact_identifier_results(query, chunks, item_by_id, query_terms=query_terms, top_k=rank_pool_size)
            if lexical_enabled
            else []
        )
        anchor_overlap_ranked = (
            self._anchor_overlap_results(query, chunks, item_by_id, top_k=rank_pool_size)
            if normalized_mode == "hybrid"
            else []
        )
        if lexical_enabled:
            lexical_ranked, lexical_ranker = self._lexical_ranked_results(query_terms, chunks, item_by_id)
        else:
            lexical_ranked, lexical_ranker = [], "disabled"

        vector_ranked: list[RetrievalResult] = []
        vector_enabled = vector_requested and self.embedding_provider is not None
        vector_error = None
        query_embedding: list[float] | None = None
        if vector_enabled and self.embedding_provider:
            try:
                query_embedding = self.embedding_provider.embed_texts([query])[0]
                for chunk, vector_score in self.store.vector_search_chunks(source_ids, query_embedding, top_k=max(top_k * 4, 20)):
                    item = item_by_id.get(chunk.source_item_id)
                    if not item:
                        continue
                    vector_ranked.append(self._result_for_chunk(chunk, item, vector_score, {"lexical": 0.0, "vector": vector_score}))
            except Exception as exc:  # noqa: BLE001 - retrieval should keep lexical fallback alive.
                query_embedding = None
                vector_error = f"{type(exc).__name__}: {exc}"

        combined = self._merge_exact_then_rrf([*exact_ranked, *exact_identifier_ranked], lexical_ranked, vector_ranked, top_k=rank_pool_size)
        combined = self._merge_scored_candidates(anchor_overlap_ranked, combined, top_k=rank_pool_size)
        combined = self._merge_priority_results(scoped_ranked, combined, top_k=rank_pool_size)
        combined = self._add_query_intent_candidates(query, combined, lexical_ranked, item_by_id, chunks, top_k=rank_pool_size)
        self._apply_query_intent_boosts(query, combined, item_by_id)
        combined = sorted(combined, key=lambda result: result.score, reverse=True)[:rank_pool_size]
        combined, graph_rank_debug = self._graph_augmented_rank(
            query,
            combined=combined,
            lexical_ranked=lexical_ranked,
            vector_ranked=vector_ranked,
            chunks=chunks,
            item_by_id=item_by_id,
            user=user,
            represented_user_id=represented_user_id,
            top_k=rank_pool_size,
            query_embedding=query_embedding,
        )
        self._apply_query_intent_boosts(query, combined, item_by_id)
        self._annotate_result_sources(combined)
        stage1_candidate_count = len(combined)
        self._apply_deterministic_evidence_scoring(
            query,
            combined,
            chunks,
            item_by_id,
            reference_time=_latest_source_time(items),
        )
        combined = sorted(combined, key=lambda result: result.score, reverse=True)[:top_k]
        for rank, result in enumerate(combined, start=1):
            result.score_debug["stage2_rank"] = float(rank)
        return combined, {
            "retrieval_mode": normalized_mode,
            "scoped_source_items": len(scoped_source_item_ids),
            "scoped_candidates": len(scoped_ranked),
            "exact_candidates": len(exact_ranked),
            "exact_identifier_candidates": len(exact_identifier_ranked),
            "anchor_overlap_candidates": len(anchor_overlap_ranked),
            "lexical_candidates": len(lexical_ranked),
            "lexical_ranker": lexical_ranker,
            "vector_enabled": vector_enabled,
            "vector_requested": vector_requested,
            "vector_candidates": len(vector_ranked),
            "vector_error": vector_error,
            "embedding_model": self.embedding_provider.model_name if self.embedding_provider else None,
            "stage1_candidate_count": stage1_candidate_count,
            "evidence_scoring_pipeline": self.evidence_score_pipeline.name,
            **graph_rank_debug,
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

    def _exact_identifier_results(
        self,
        query: str,
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
        *,
        query_terms: list[str],
        top_k: int,
    ) -> list[RetrievalResult]:
        identifiers = _query_exact_identifiers(query)
        if not identifiers:
            return []
        candidates: list[RetrievalResult] = []
        for chunk in chunks:
            item = item_by_id.get(chunk.source_item_id)
            if item is None:
                continue
            haystack = f"{item.title}\n{chunk.text}\n{item.url or ''}".casefold()
            matched = [identifier for identifier in identifiers if identifier in haystack]
            if not matched:
                continue
            lexical = self._lexical_score(query_terms, self._terms(f"{item.title} {chunk.text} {item.url or ''}"))
            score = 1.5 + len(matched) * 0.08 + min(lexical, 1.0) * 0.05
            candidates.append(
                self._result_for_chunk(
                    chunk,
                    item,
                    score,
                    {"exact_identifier": float(len(matched)), "lexical": lexical, "vector": 0.0},
                )
            )
        candidates.sort(key=lambda result: (result.score, -result.score_debug.get("chunk_ordinal", 0.0)), reverse=True)
        return candidates[:top_k]

    def _scoped_source_results(
        self,
        scoped_source_item_ids: set[str],
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
        *,
        query_terms: list[str],
        top_k: int,
    ) -> list[RetrievalResult]:
        if not scoped_source_item_ids:
            return []
        candidates: list[RetrievalResult] = []
        for index, chunk in enumerate(chunks):
            if chunk.source_item_id not in scoped_source_item_ids:
                continue
            item = item_by_id.get(chunk.source_item_id)
            if item is None:
                continue
            text_terms = self._terms(f"{item.title} {chunk.text} {item.url or ''}")
            lexical = self._lexical_score(query_terms, text_terms)
            score = 1.25 + min(lexical, 1.0) * 0.2 - index * 0.001
            candidates.append(
                self._result_for_chunk(
                    chunk,
                    item,
                    score,
                    {"scope": 1.0, "lexical": lexical, "vector": 0.0},
                )
            )
        candidates.sort(key=lambda result: result.score, reverse=True)
        source_counts: dict[str, int] = {}
        results: list[RetrievalResult] = []
        for result in candidates:
            if source_counts.get(result.source_item_id, 0) >= 2:
                continue
            source_counts[result.source_item_id] = source_counts.get(result.source_item_id, 0) + 1
            results.append(result)
            if len(results) >= top_k:
                break
        return results

    def _anchor_overlap_results(
        self,
        query: str,
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
        *,
        top_k: int,
    ) -> list[RetrievalResult]:
        anchors = _snippet_anchor_terms(query)
        if not anchors:
            return []
        chunks_by_document: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            chunks_by_document.setdefault(chunk.document_id, []).append(chunk)
        for document_chunks in chunks_by_document.values():
            document_chunks.sort(key=lambda chunk: int(chunk.ordinal or 0))
        candidates: list[RetrievalResult] = []
        asks_for_value = _query_seeks_numeric_answer(query)
        for chunk in chunks:
            item = item_by_id.get(chunk.source_item_id)
            if item is None:
                continue
            context_text = self._chunk_text_with_table_header(chunk, chunks_by_document.get(chunk.document_id, []))
            haystack = f"{item.title}\n{context_text}"
            coverage = _anchor_coverage(anchors, haystack)
            hit_count = _anchor_hit_count(anchors, haystack)
            if coverage < 0.28 and hit_count < 3:
                continue
            score = 0.16 + min(coverage * 0.05, 0.05) + min(hit_count * 0.004, 0.024)
            if asks_for_value and _text_has_numeric_evidence(context_text):
                score += 0.018
            if _looks_like_tabular_evidence(context_text):
                score += 0.012
            candidates.append(
                self._result_for_chunk(
                    chunk,
                    item,
                    score,
                    {
                        "anchor_overlap": coverage,
                        "anchor_hit_count": float(hit_count),
                        "lexical": 0.0,
                        "vector": 0.0,
                    },
                )
            )
        candidates.sort(key=lambda result: (result.score, result.score_debug.get("anchor_hit_count", 0.0)), reverse=True)
        for rank, result in enumerate(candidates[:top_k], start=1):
            result.score_debug["anchor_overlap_rank"] = float(rank)
        return candidates[:top_k]

    def _result_for_chunk(self, chunk: Chunk, item: SourceItem, score: float, score_debug: dict[str, float]) -> RetrievalResult:
        score_debug = {**score_debug, "chunk_ordinal": float(chunk.ordinal)}
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
            source=_primary_result_source(score_debug),
            title=item.title,
            snippet=chunk.text[:240],
            score=score,
            citation=citation,
            score_debug=score_debug,
        )

    def _annotate_result_sources(self, results: list[RetrievalResult]) -> None:
        for result in results:
            result.source = _primary_result_source(result.score_debug)

    def _terms(self, text: str) -> list[str]:
        raw_tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
        terms: list[str] = []
        for token in raw_tokens:
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                terms.append(token)
                for ngram_size in (2, 3):
                    if len(token) >= ngram_size:
                        terms.extend(token[index : index + ngram_size] for index in range(len(token) - ngram_size + 1))
            else:
                terms.append(token)
        return terms

    def _lexical_score(self, query_terms: list[str], text_terms: list[str]) -> float:
        if not query_terms or not text_terms:
            return 0.0
        counts = {term: text_terms.count(term) for term in set(query_terms)}
        return sum(counts.values()) / math.sqrt(len(text_terms))

    def _lexical_ranked_results(
        self,
        query_terms: list[str],
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
    ) -> tuple[list[RetrievalResult], str]:
        chunks_by_document: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            chunks_by_document.setdefault(chunk.document_id, []).append(chunk)
        for document_chunks in chunks_by_document.values():
            document_chunks.sort(key=lambda chunk: int(chunk.ordinal or 0))
        documents = [
            self._terms(
                f"{item_by_id[chunk.source_item_id].title} "
                f"{self._chunk_text_with_table_header(chunk, chunks_by_document.get(chunk.document_id, []))} "
                f"{item_by_id[chunk.source_item_id].url or ''}"
            )
            for chunk in chunks
        ]
        fallback_results: list[RetrievalResult] = []
        for chunk, text_terms in zip(chunks, documents):
            item = item_by_id[chunk.source_item_id]
            lexical = self._lexical_score(query_terms, text_terms)
            if lexical <= 0:
                continue
            fallback_results.append(self._result_for_chunk(chunk, item, lexical, {"lexical": lexical, "vector": 0.0}))

        bm25_scores = _bm25_scores(documents, query_terms)
        if bm25_scores is not None:
            results = [
                self._result_for_chunk(chunk, item_by_id[chunk.source_item_id], score, {"lexical": score, "bm25": score, "vector": 0.0})
                for chunk, score in zip(chunks, bm25_scores)
                if score > 0
            ]
            if results or not fallback_results:
                return sorted(results, key=lambda result: result.score, reverse=True), "rank_bm25"
            return sorted(fallback_results, key=lambda result: result.score, reverse=True), "rank_bm25_term_frequency_fallback"

        return sorted(fallback_results, key=lambda result: result.score, reverse=True), "term_frequency"

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

    def _merge_priority_results(
        self,
        priority_ranked: list[RetrievalResult],
        combined: list[RetrievalResult],
        *,
        top_k: int,
    ) -> list[RetrievalResult]:
        if not priority_ranked:
            return combined[:top_k]
        seen: set[str] = set()
        merged: list[RetrievalResult] = []
        for result in [*priority_ranked, *combined]:
            if result.result_id in seen:
                continue
            seen.add(result.result_id)
            merged.append(result)
            if len(merged) >= top_k:
                break
        return merged

    def _merge_scored_candidates(
        self,
        additions: list[RetrievalResult],
        combined: list[RetrievalResult],
        *,
        top_k: int,
    ) -> list[RetrievalResult]:
        if not additions:
            return combined[:top_k]
        by_id = {result.result_id: result for result in combined}
        for addition in additions:
            existing = by_id.get(addition.result_id)
            if existing is None:
                by_id[addition.result_id] = addition
                continue
            if addition.score > existing.score:
                existing.score = addition.score
            for key, value in addition.score_debug.items():
                existing.score_debug[key] = max(existing.score_debug.get(key, 0.0), value)
        return sorted(by_id.values(), key=lambda result: result.score, reverse=True)[:top_k]

    def _add_query_intent_candidates(
        self,
        query: str,
        combined: list[RetrievalResult],
        lexical_ranked: list[RetrievalResult],
        item_by_id: dict[str, SourceItem],
        chunks: list[Chunk],
        *,
        top_k: int,
    ) -> list[RetrievalResult]:
        seen = {result.result_id for result in combined}
        additions: list[RetrievalResult] = []
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        metric_phrases = _query_metric_phrases(query)
        if metric_phrases:
            metric_ranked: list[tuple[float, RetrievalResult]] = []
            for result in lexical_ranked:
                chunk = chunk_by_id.get(result.result_id)
                if chunk is None:
                    continue
                match_score = _metric_phrase_match_score(metric_phrases, chunk.text)
                if match_score <= 0:
                    continue
                boost = 0.05 + min(match_score * 0.01, 0.08)
                result.score += boost
                result.score_debug["metric_phrase_match"] = float(match_score)
                result.score_debug["metric_phrase_boost"] = boost
                metric_ranked.append((match_score, result))
            metric_ranked.sort(key=lambda item: (item[0], item[1].score), reverse=True)
            for _match_score, result in metric_ranked[: max(1, min(3, top_k))]:
                if result.result_id in seen:
                    continue
                additions.append(result)
                seen.add(result.result_id)
        if _spreadsheet_query_intent(query):
            for result in lexical_ranked:
                if result.result_id in seen:
                    continue
                item = item_by_id.get(result.source_item_id)
                if item is None or not _is_spreadsheet_source(item):
                    continue
                additions.append(result)
                seen.add(result.result_id)
                if len(additions) >= max(1, min(2, top_k)):
                    break
        position_intent = _document_position_intent(query)
        if position_intent:
            position_results = self._document_position_results(query, combined, lexical_ranked, item_by_id, position_intent=position_intent)
            for result in position_results:
                boosted_score = max(result.score, (combined[0].score if combined else 0.0) + 0.025)
                result.score = boosted_score
                result.score_debug["document_position_intent"] = 1.0
                result.score_debug["document_position"] = 1.0 if position_intent == "tail" else 0.0
                if result.result_id in seen:
                    continue
                additions.append(result)
                seen.add(result.result_id)
                if len(additions) >= max(1, min(3, top_k)):
                    break
        return [*combined, *additions]

    def _document_position_results(
        self,
        query: str,
        combined: list[RetrievalResult],
        lexical_ranked: list[RetrievalResult],
        item_by_id: dict[str, SourceItem],
        *,
        position_intent: str,
    ) -> list[RetrievalResult]:
        query_terms = set(self._terms(query))
        source_ids: set[str] = {result.source_item_id for result in combined}
        for result in lexical_ranked[:50]:
            item = item_by_id.get(result.source_item_id)
            if item is None:
                continue
            title_terms = set(self._terms(f"{item.title} {item.url or ''}"))
            if query_terms.intersection(title_terms):
                source_ids.add(result.source_item_id)
        if not source_ids:
            return []
        by_source: dict[str, list[RetrievalResult]] = {}
        for result in lexical_ranked:
            if result.source_item_id in source_ids:
                by_source.setdefault(result.source_item_id, []).append(result)
        selected: list[RetrievalResult] = []
        for results in by_source.values():
            if position_intent == "tail":
                selected.append(max(results, key=lambda result: result.score_debug.get("chunk_ordinal", 0.0)))
            else:
                selected.append(min(results, key=lambda result: result.score_debug.get("chunk_ordinal", 0.0)))
        return sorted(selected, key=lambda result: result.score, reverse=True)

    def _apply_query_intent_boosts(
        self,
        query: str,
        results: list[RetrievalResult],
        item_by_id: dict[str, SourceItem],
    ) -> None:
        document_years = _query_document_years(query)
        if document_years:
            for result in results:
                item = item_by_id.get(result.source_item_id)
                if item is None:
                    continue
                source_years = set(_source_document_years(item))
                if source_years.intersection(document_years):
                    boost = 0.08
                    result.score += boost
                    result.score_debug["document_year_match"] = boost
                elif source_years:
                    penalty = 0.03
                    result.score -= penalty
                    result.score_debug["document_year_mismatch_penalty"] = penalty
        elif _query_requests_latest(query):
            latest_year = _latest_document_year(item_by_id.values())
            if latest_year:
                for result in results:
                    item = item_by_id.get(result.source_item_id)
                    if item is None:
                        continue
                    source_years = set(_source_document_years(item))
                    if latest_year in source_years:
                        boost = 0.07
                        result.score += boost
                        result.score_debug["latest_document_year_match"] = boost
                    elif source_years:
                        penalty = 0.02
                        result.score -= penalty
                        result.score_debug["latest_document_year_mismatch_penalty"] = penalty
        if not _spreadsheet_query_intent(query):
            return
        for result in results:
            item = item_by_id.get(result.source_item_id)
            if item is None or not _is_spreadsheet_source(item):
                continue
            if result.score_debug.get("query_intent_boost"):
                continue
            boost = 0.035
            result.score += boost
            result.score_debug["query_intent_boost"] = boost
            result.score_debug["spreadsheet_intent_match"] = 1.0

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

    def _apply_deterministic_evidence_scoring(
        self,
        query: str,
        results: list[RetrievalResult],
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
        *,
        reference_time: datetime | None,
    ) -> None:
        for rank, result in enumerate(sorted(results, key=lambda item: item.score, reverse=True), start=1):
            result.score_debug["stage1_rank"] = float(rank)
            result.score_debug["stage1_score"] = result.score
        self._apply_rank_quality_boosts(results, item_by_id, reference_time=reference_time)
        self._apply_query_focused_snippets(query, results, chunks)
        self._apply_evidence_policy_scores(query, results)
        for result in results:
            stage1_score = float(result.score_debug.get("stage1_score", result.score))
            delta = result.score - stage1_score
            result.score_debug["evidence_scoring_delta"] = delta
            result.score_debug["stage2_evidence_delta"] = delta
        results.sort(key=lambda result: result.score, reverse=True)

    def _apply_evidence_policy_scores(self, query: str, results: list[RetrievalResult]) -> None:
        context = EvidenceScoreContext(
            query=query,
            anchors=tuple(_snippet_anchor_terms(query)),
            asks_for_numeric_answer=_query_seeks_numeric_answer(query),
        )
        for result in results:
            outcome = self.evidence_score_pipeline.score(result, context)
            if outcome.debug:
                result.score_debug.update(outcome.debug)
            if outcome.score:
                result.score += outcome.score

    def _apply_query_focused_snippets(self, query: str, results: list[RetrievalResult], chunks: list[Chunk]) -> None:
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        chunks_by_document: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            chunks_by_document.setdefault(chunk.document_id, []).append(chunk)
        for document_chunks in chunks_by_document.values():
            document_chunks.sort(key=lambda chunk: int(chunk.ordinal or 0))
        terms = [term for term in self._terms(query) if len(term) >= 3]
        for result in results:
            chunk = chunk_by_id.get(result.result_id)
            if chunk is None:
                continue
            context_text = self._chunk_text_with_table_header(chunk, chunks_by_document.get(chunk.document_id, []))
            snippet = query_focused_evidence_snippet(context_text, query, max_chars=1200)
            if not snippet:
                continue
            result.snippet = snippet
            result.citation["snippet"] = snippet
            result.score_debug["query_focused_snippet"] = 1.0
            coverage = _focused_snippet_query_coverage(snippet, terms)
            if coverage:
                boost = min(coverage * 0.002, 0.02)
                result.score += boost
                result.score_debug["focused_snippet_query_coverage"] = float(coverage)
                result.score_debug["focused_snippet_boost"] = boost
        results.sort(key=lambda result: result.score, reverse=True)

    def _chunk_text_with_table_header(self, chunk: Chunk, document_chunks: list[Chunk]) -> str:
        text = str(chunk.text or "")
        if not text or _text_has_table_header(text):
            return text
        if "|" not in text:
            return text
        header = _nearest_previous_table_header(chunk, document_chunks)
        if not header:
            return text
        if not _text_has_table_row_with_cell_count(text, len(_table_cells(header))):
            return text
        return f"{header}\n{text}"

    def _graph_augmented_rank(
        self,
        query: str,
        *,
        combined: list[RetrievalResult],
        lexical_ranked: list[RetrievalResult],
        vector_ranked: list[RetrievalResult],
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
        user: User,
        represented_user_id: str | None,
        top_k: int,
        query_embedding: list[float] | None,
    ) -> tuple[list[RetrievalResult], dict[str, Any]]:
        graph = self._build_retrieval_graph(
            query=query,
            combined=combined,
            lexical_ranked=lexical_ranked,
            vector_ranked=vector_ranked,
            chunks=chunks,
            item_by_id=item_by_id,
            user=user,
            represented_user_id=represented_user_id,
            query_embedding=query_embedding,
        )
        if (
            not graph["seeds"]
            or not graph["adjacency"]
            or not graph["edge_count"]
            or not (graph.get("fact_seed_count") or graph.get("query_entity_seed_count"))
        ):
            return combined, {
                "graph_ranker": "rag_fallback",
                "graph_ppr_enabled": False,
                "graph_ppr_nodes": 0,
                "graph_ppr_edges": graph["edge_count"],
                "graph_ppr_seed_count": len(graph["seeds"]),
                "graph_fact_seed_count": graph.get("fact_seed_count", 0),
                "graph_query_entity_seed_count": graph.get("query_entity_seed_count", 0),
                "hipporag_offline_graph": graph.get("offline_graph", {}),
                "hipporag_embedding_linking": graph.get("embedding_linking", {}),
                "graph_expanded_candidates": 0,
            }

        ppr_scores = _personalized_pagerank(graph["adjacency"], graph["seeds"])
        if not ppr_scores:
            return combined, {
                "graph_ranker": "rag_fallback",
                "graph_ppr_enabled": False,
                "graph_ppr_nodes": len(graph["adjacency"]),
                "graph_ppr_edges": graph["edge_count"],
                "graph_ppr_seed_count": len(graph["seeds"]),
                "graph_fact_seed_count": graph.get("fact_seed_count", 0),
                "graph_query_entity_seed_count": graph.get("query_entity_seed_count", 0),
                "hipporag_offline_graph": graph.get("offline_graph", {}),
                "hipporag_embedding_linking": graph.get("embedding_linking", {}),
                "graph_expanded_candidates": 0,
            }

        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        by_id = {result.result_id: result for result in combined}
        expanded = 0
        for node_id, ppr_score in sorted(ppr_scores.items(), key=lambda item: item[1], reverse=True):
            if not node_id.startswith("chunk:") or ppr_score <= 0:
                continue
            chunk_id = node_id.removeprefix("chunk:")
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            is_expanded_candidate = chunk_id not in by_id
            if is_expanded_candidate:
                item = item_by_id.get(chunk.source_item_id)
                if item is None:
                    continue
                by_id[chunk_id] = self._result_for_chunk(
                    chunk,
                    item,
                    0.0,
                    {
                        "lexical": 0.0,
                        "vector": 0.0,
                        "graph_ppr": 0.0,
                        "graph_expansion": 1.0,
                    },
                )
                expanded += 1
            result = by_id[chunk_id]
            base_score = result.score
            graph_boost = min(ppr_score * 0.18, 0.012) if is_expanded_candidate else min(ppr_score * 0.25, 0.018)
            result.score = base_score + graph_boost
            result.score_debug["graph_ppr"] = ppr_score
            result.score_debug["graph_boost"] = graph_boost
            result.score_debug["pre_graph_score"] = result.score_debug.get("pre_graph_score", base_score)

        ranked = sorted(
            by_id.values(),
            key=lambda result: (
                result.score,
                result.score_debug.get("graph_ppr", 0.0),
                result.result_id,
            ),
            reverse=True,
        )[:top_k]
        entity_scores = {
            node_id.removeprefix("entity:"): score
            for node_id, score in ppr_scores.items()
            if node_id.startswith("entity:")
        }
        return ranked, {
            "graph_ranker": "ppr_chunk_entity_fusion",
            "graph_ppr_enabled": True,
            "graph_ppr_nodes": len(graph["adjacency"]),
            "graph_ppr_edges": graph["edge_count"],
            "graph_ppr_seed_count": len(graph["seeds"]),
            "graph_fact_seed_count": graph.get("fact_seed_count", 0),
            "graph_query_entity_seed_count": graph.get("query_entity_seed_count", 0),
            "hipporag_offline_graph": graph.get("offline_graph", {}),
            "hipporag_embedding_linking": graph.get("embedding_linking", {}),
            "graph_expanded_candidates": expanded,
            "graph_top_facts": graph.get("top_facts", []),
            "graph_top_entities": [
                {"entity_id": entity_id, "score": round(score, 8)}
                for entity_id, score in sorted(entity_scores.items(), key=lambda item: item[1], reverse=True)[:5]
            ],
        }

    def _build_retrieval_graph(
        self,
        *,
        query: str,
        combined: list[RetrievalResult],
        lexical_ranked: list[RetrievalResult],
        vector_ranked: list[RetrievalResult],
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
        user: User,
        represented_user_id: str | None,
        query_embedding: list[float] | None,
    ) -> dict[str, Any]:
        all_entities = self.store.list_entities(tenant_id=user.tenant_id)
        visible_entities = self._visible_entities(all_entities, user=user, represented_user_id=represented_user_id)
        entity_by_id = {entity.entity_id: entity for entity in visible_entities}
        edge_ids_seen: set[str] = set()
        visible_hyperedges: list[tuple[Hyperedge, list[HyperedgeMember]]] = []
        if entity_by_id:
            for edge, members in self.store.list_hyperedges_for_entities(set(entity_by_id)):
                if edge.hyperedge_id in edge_ids_seen:
                    continue
                edge_ids_seen.add(edge.hyperedge_id)
                if not self._can_read_graph_object(user, edge.owner_user_id, edge.visibility, edge.visible_team_ids, represented_user_id):
                    continue
                visible_hyperedges.append((edge, members))
        visible_claims = self.store.list_knowledge_claims(
            owner_user_id=represented_user_id or user.user_id,
            tenant_id=user.tenant_id,
            source_item_ids=set(item_by_id),
            limit=200,
        )

        offline_index = HippoRAGOfflineIndex.build(
            entities=visible_entities,
            hyperedges=visible_hyperedges,
            knowledge_claims=visible_claims,
            chunks=chunks,
            item_by_id=item_by_id,
        )
        embedding_error = None
        graph_embedding_linking_enabled = self.graph_embedding_linking and self.embedding_provider is not None
        if graph_embedding_linking_enabled:
            try:
                offline_index.with_embeddings(self.embedding_provider)
                if query_embedding is None:
                    query_embedding = self.embedding_provider.embed_texts([query])[0]
            except Exception as exc:  # noqa: BLE001 - retrieval should keep lexical fallback alive.
                query_embedding = None
                embedding_error = f"{type(exc).__name__}: {exc}"
        adjacency: dict[str, dict[str, float]] = {
            node_id: dict(neighbors)
            for node_id, neighbors in offline_index.adjacency.items()
        }
        seeds: dict[str, float] = {}

        def add_seed(node_id: str, weight: float) -> None:
            if weight > 0 and node_id in adjacency:
                seeds[node_id] = seeds.get(node_id, 0.0) + weight

        # HippoRAG 2 style: dense passages participate in PPR, but with a small
        # passage-node prior so graph/fact seeds can still move probability mass.
        for rank, result in enumerate(combined, start=1):
            add_seed(f"chunk:{result.result_id}", 0.05 / rank)
        for rank, result in enumerate(lexical_ranked[:10], start=1):
            add_seed(f"chunk:{result.result_id}", 0.02 / rank)
        for rank, result in enumerate(vector_ranked[:10], start=1):
            add_seed(f"chunk:{result.result_id}", 0.02 / rank)

        normalized_query = _normalize_match_text(query)
        query_entity_seed_count = 0
        entity_links = offline_index.link_entities(query, query_embedding=query_embedding, limit=8)
        linked_entity_ids = {link.entity_id for link in entity_links}
        for entity in visible_entities:
            entity_node = f"entity:{entity.entity_id}"
            query_mentioned = any(
                _match_position(normalized_query, _normalize_match_text(alias)) is not None
                or _fuzzy_alias_match(normalized_query, _normalize_match_text(alias))
                for alias in _entity_aliases(entity)
            )
            if query_mentioned or entity.entity_id in linked_entity_ids:
                link_score = next((link.score for link in entity_links if link.entity_id == entity.entity_id), 0.0)
                add_seed(entity_node, max(0.35 if query_mentioned else 0.0, link_score))
                query_entity_seed_count += 1

        if not entity_by_id:
            return {
                "adjacency": adjacency,
                "seeds": seeds,
                "edge_count": _adjacency_edge_count(adjacency),
                "offline_graph": offline_index.graph_info,
                "fact_seed_count": 0,
                "query_entity_seed_count": query_entity_seed_count,
            }

        top_fact_items = offline_index.score_facts(query, limit=8, query_embedding=query_embedding)
        fact_seed_totals: dict[str, float] = {}
        fact_seed_counts: dict[str, int] = {}
        for item in top_fact_items:
            fact = item["fact"]
            fact_score = float(item["score"])
            if fact_score <= 0:
                continue
            add_seed(fact.fact_id, fact_score)
            for entity_id in fact.member_entity_ids:
                fact_seed_totals[entity_id] = fact_seed_totals.get(entity_id, 0.0) + fact_score
                fact_seed_counts[entity_id] = fact_seed_counts.get(entity_id, 0) + 1
        for entity_id, total in fact_seed_totals.items():
            add_seed(f"entity:{entity_id}", total / max(fact_seed_counts.get(entity_id, 1), 1))

        return {
            "adjacency": adjacency,
            "seeds": seeds,
            "edge_count": _adjacency_edge_count(adjacency),
            "offline_graph": offline_index.graph_info,
            "embedding_linking": {
                "enabled": graph_embedding_linking_enabled and query_embedding is not None,
                "error": embedding_error,
                "fact_embeddings": len(offline_index.fact_embeddings),
                "entity_embeddings": len(offline_index.entity_embeddings),
                "linked_entities": [
                    {
                        "entity_id": link.entity_id,
                        "label": link.label,
                        "score": round(link.score, 8),
                        "score_debug": link.score_debug,
                    }
                    for link in entity_links[:5]
                ],
            },
            "fact_seed_count": len(top_fact_items),
            "query_entity_seed_count": query_entity_seed_count,
            "top_facts": [item["summary"] for item in top_fact_items[:5]],
        }

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
        for card in self.store.list_profile_cards(owner_user_id=owner_user_id, tenant_id=user.tenant_id):
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
        for memory in self.store.list_agent_memories(owner_user_id=owner_user_id, tenant_id=user.tenant_id):
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
        entities = self._matching_entities(query, ranked, tenant_id=user.tenant_id)
        if _is_graph_global_query(query):
            entities = self.store.list_entities(tenant_id=user.tenant_id)
        visible_entities = self._visible_entities(entities, user=user, represented_user_id=represented_user_id)
        edges = self.store.list_hyperedges_for_entities({entity.entity_id for entity in visible_entities})
        context = []
        entity_by_id = {entity.entity_id: entity for entity in self.store.list_entities(tenant_id=user.tenant_id)}
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
            self._matching_entities(query, ranked, tenant_id=user.tenant_id),
            user=user,
            represented_user_id=represented_user_id,
        )
        if not seed_entities:
            return []

        all_entities = self.store.list_entities(tenant_id=user.tenant_id)
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

    def _matching_entities(self, query: str, ranked: list[RetrievalResult], *, tenant_id: str | None = None) -> list[Entity]:
        haystack = " ".join([query, *[result.title for result in ranked], *[result.snippet for result in ranked]])
        normalized_haystack = _normalize_match_text(haystack)
        matches: list[tuple[int, int, str, Entity]] = []
        for entity in self.store.list_entities(tenant_id=tenant_id):
            for alias in _entity_aliases(entity):
                normalized_alias = _normalize_match_text(alias)
                if not normalized_alias:
                    continue
                position = _match_position(normalized_haystack, normalized_alias)
                if position is None and not _fuzzy_alias_match(normalized_haystack, normalized_alias):
                    continue
                matches.append(
                    (
                        position if position is not None else len(normalized_haystack) + 1,
                        -len(normalized_alias),
                        entity.entity_id,
                        entity,
                    )
                )
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
            if entity.tenant_id == user.tenant_id
            and self._can_read_graph_object(user, entity.owner_user_id, entity.visibility, entity.visible_team_ids, represented_user_id)
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
            return bool(self.acl.visible_team_ids_for_user(effective_user_id, tenant_id=user.tenant_id).intersection(visible_team_ids))
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
            for item in self.store.list_source_items(tenant_id=user.tenant_id)
            if item.source_item_id in source_item_ids
            and _is_active_source_item(item)
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
            for item in self.store.list_source_items(tenant_id=user.tenant_id)
            if item.source_item_id in source_item_ids
            and _is_active_source_item(item)
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


def _is_active_source_item(item: Any) -> bool:
    return str(getattr(item, "lifecycle_status", "active") or "active") == "active"


def _context_owner_user_id(user: User, represented_user_id: str | None) -> str:
    if user.role == "agent_service" and represented_user_id:
        return represented_user_id
    return represented_user_id or user.user_id


def _normalize_exact(value: str) -> str:
    return value.strip().lower()


def _query_exact_identifiers(query: str) -> list[str]:
    identifiers: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b[a-z][a-z0-9]{1,}(?:[-_][a-z0-9]{2,})+\b|\b[a-z]{2,}\d{2,}[a-z0-9_-]*\b", str(query or ""), flags=re.IGNORECASE):
        identifier = match.group(0).strip("._-").casefold()
        if len(identifier) < 5 or identifier in seen or not re.search(r"\d", identifier):
            continue
        seen.add(identifier)
        identifiers.append(identifier)
    return identifiers[:12]


def _query_metric_phrases(query: str) -> list[str]:
    segment = str(query or "")
    for marker in ("分别是什么", "分别是多少", "是什么", "是多少", "有哪些", "多少", "吗", "?", "？"):
        index = segment.find(marker)
        if index >= 0:
            segment = segment[:index]
            break
    if "：" in segment or ":" in segment:
        parts = re.split(r"[:：]", segment, maxsplit=1)
        if len(parts) == 2 and any(marker in parts[0] for marker in ("回答", "请", "问题", "question", "answer")):
            segment = parts[1]
    scope_marker = re.search(r"(?:表中|报告中|年报中|资料中|文档中|知识库中|附件中|文件中|中)[,，:：]?\s*(.+)$", segment)
    if scope_marker and scope_marker.group(1).strip():
        segment = scope_marker.group(1).strip()
    normalized = re.sub(r"\b(?:and|plus|or)\b", "、", segment, flags=re.IGNORECASE)
    normalized = normalized.replace("以及", "、").replace("及", "、").replace("和", "、")
    normalized = re.sub(r"[,，/;；\n]+", "、", normalized)
    phrases: list[str] = []
    seen: set[str] = set()
    for raw_part in normalized.split("、"):
        phrase = re.sub(r"^(?:请问|请|帮我|告诉我|基于|根据|按照|在|从)\s*", "", raw_part.strip())
        phrase = re.sub(r"^(?:\d{4}\s*年(?:度)?|20\d{2})\s*的?", "", phrase)
        phrase = phrase.strip(" \t\r\n'\"“”‘’[]【】{}<>《》:：")
        normalized_phrase = _normalize_metric_phrase(phrase)
        if len(normalized_phrase) < 3 or normalized_phrase in seen:
            continue
        if normalized_phrase in {"资料", "信息", "内容", "答案", "结论", "结果", "字段", "数字", "数值"}:
            continue
        seen.add(normalized_phrase)
        phrases.append(phrase)
        if len(phrases) >= 8:
            break
    return phrases


def _query_document_years(query: str) -> set[str]:
    text = str(query or "")
    years: set[str] = set()
    raw_years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)
    if len(set(raw_years)) >= 2:
        years.update(raw_years)
    for start, end in re.findall(r"(?<!\d)(20\d{2})(?!\d)\s*(?:年)?\s*(?:至|到|-|~|—|–|through|to)\s*(?<!\d)(20\d{2})(?!\d)\s*(?:年)?", text, flags=re.IGNORECASE):
        start_year = int(start)
        end_year = int(end)
        if start_year > end_year:
            start_year, end_year = end_year, start_year
        if end_year - start_year <= 20:
            years.update(str(year) for year in range(start_year, end_year + 1))
    for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)\s*年(?:度)?\s*(?:年度报告|年报|报告)", text):
        years.add(match.group(1))
    for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)[^\n。！？?]{0,20}\bannual\s+report\b", text, flags=re.IGNORECASE):
        years.add(match.group(1))
    for match in re.finditer(r"\bannual\s+report\b[^\n。！？?]{0,20}(?<!\d)(20\d{2})(?!\d)", text, flags=re.IGNORECASE):
        years.add(match.group(1))
    if len(set(raw_years)) == 1 and any(marker in text for marker in ("年报", "年度报告", "报告", "季度", "年度", "最新")):
        years.add(raw_years[0])
    return years


def _query_requests_latest(query: str) -> bool:
    text = str(query or "").casefold()
    return any(marker in text for marker in ("最新", "最近", "最新的", "latest", "most recent", "newest"))


def _source_document_years(item: SourceItem) -> list[str]:
    metadata = item.metadata or {}
    text = " ".join(
        str(value or "")
        for value in (
            item.title,
            item.source_id,
            item.url or "",
            metadata.get("filename"),
            metadata.get("path"),
        )
    )
    return list(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)))


def _latest_document_year(items: Any) -> str | None:
    years: list[str] = []
    for item in items:
        years.extend(_source_document_years(item))
    if not years:
        return None
    return max(years)


def _metric_phrase_match_score(phrases: list[str], text: str) -> int:
    normalized_text = _normalize_metric_phrase(text)
    if not normalized_text:
        return 0
    score = 0
    for phrase in phrases:
        normalized_phrase = _normalize_metric_phrase(phrase)
        if not normalized_phrase or normalized_phrase not in normalized_text:
            continue
        score += max(2, min(len(normalized_phrase) // 3, 8))
    return score


def _normalize_metric_phrase(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\([^)]*\)|（[^）]*）", "", text)
    return re.sub(r"[\s_\-:/：、,，.。;；|\"'“”‘’]+", "", text)


def _spreadsheet_query_intent(query: str) -> bool:
    terms = {term.casefold() for term in re.findall(r"[\w\u4e00-\u9fff]+", query)}
    return bool(
        terms.intersection(
            {
                "excel",
                "xlsx",
                "xls",
                "spreadsheet",
                "spreadsheets",
                "workbook",
                "workbooks",
                "sheet",
                "sheets",
                "表格",
                "电子表格",
                "工作簿",
            }
        )
    )


def _document_position_intent(query: str) -> str | None:
    normalized = str(query or "").casefold()
    tail_markers = (
        "last page",
        "final page",
        "back page",
        "end of document",
        "最后一页",
        "最后页",
        "末页",
        "尾页",
        "文末",
    )
    head_markers = (
        "first page",
        "cover page",
        "front page",
        "beginning of document",
        "第一页",
        "首页",
        "封面",
        "文首",
    )
    if any(marker in normalized for marker in tail_markers):
        return "tail"
    if any(marker in normalized for marker in head_markers):
        return "head"
    return None


def _is_spreadsheet_source(item: SourceItem) -> bool:
    fields = [item.title, item.url, item.source_id]
    raw_paths = item.metadata.get("raw_paths") if isinstance(item.metadata, dict) else None
    if isinstance(raw_paths, dict):
        fields.extend(str(value) for value in raw_paths.values())
    extraction = item.metadata.get("extraction") if isinstance(item.metadata, dict) else None
    if isinstance(extraction, dict):
        fields.append(str(extraction.get("extractor") or ""))
    haystack = " ".join(str(value or "") for value in fields).casefold()
    return any(token in haystack for token in (".xlsx", ".xls", "xlsx-", "spreadsheet", "workbook"))


def query_focused_evidence_snippet(text: str, query: str, *, max_chars: int = 420) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    anchors = _snippet_anchor_terms(query)
    table = _focused_table_snippet(text, anchors, max_chars=max_chars)
    if table:
        return table
    if len(text) <= max_chars:
        return text
    index = _best_anchor_index(text, anchors)
    if index < 0:
        return text[:max_chars].rstrip()
    start = max(0, index - max_chars // 3)
    end = min(len(text), start + max_chars)
    start, end = _expand_snippet_to_line_boundaries(text, start, end, max_chars=max_chars)
    return text[start:end].strip()


def _snippet_anchor_terms(query: str) -> list[str]:
    normalized = str(query or "").casefold()
    raw_terms = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)+|[a-z0-9_]+|[\u4e00-\u9fff]{2,}", normalized)
    stopwords = {
        "what",
        "which",
        "where",
        "when",
        "last",
        "first",
        "page",
        "final",
        "row",
        "rows",
        "column",
        "columns",
        "please",
        "answer",
        "多少",
        "是什么",
        "是多少",
        "相关",
        "问题",
        "回答",
        "引用",
        "来源",
        "最后",
        "最后一页",
        "第一页",
        "附件",
        "文档",
        "资料",
        "表格",
    }
    anchors: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        term = term.strip(" _-")
        candidates = _anchor_term_candidates(term)
        for candidate in candidates:
            if len(candidate) < 2 or candidate in stopwords or candidate in seen:
                continue
            seen.add(candidate)
            anchors.append(candidate)
    return sorted(anchors, key=lambda value: (bool(re.search(r"\d", value)), len(value)), reverse=True)[:24]


def _anchor_term_candidates(term: str) -> list[str]:
    if not re.fullmatch(r"[\u4e00-\u9fff]+", term) or len(term) <= 4:
        return [term]
    cleaned = re.sub(r"(?:请|回答|引用|来源|根据|基于|是多少|是什么|多少|哪个|哪些|一下)", " ", term)
    segments = [segment for segment in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned) if segment]
    candidates = [term, *segments]
    for segment in segments:
        for ngram_size in (4, 3, 2):
            if len(segment) >= ngram_size:
                candidates.extend(segment[index : index + ngram_size] for index in range(len(segment) - ngram_size + 1))
    return list(dict.fromkeys(candidates))


def _focused_table_snippet(text: str, anchors: list[str], *, max_chars: int) -> str:
    if not anchors or "|" not in text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matched = [
        (index, _line_anchor_score(line, anchors))
        for index, line in enumerate(lines)
        if _is_table_content_line(line)
    ]
    matched = [(index, score) for index, score in matched if score > 0]
    if not matched:
        return ""
    matched_indices = [index for index, _score in sorted(matched, key=lambda item: (item[1], -item[0]), reverse=True)]
    rendered: list[str] = []
    seen: set[str] = set()
    for row_index in matched_indices[:4]:
        header_index = _table_header_index(lines, row_index)
        row = lines[row_index]
        if header_index is not None and header_index != row_index:
            focused = _render_focused_table_row(lines[header_index], row, anchors, max_chars=max_chars)
            parts = focused.splitlines() if focused else [lines[header_index], row]
        else:
            parts = [row]
        for part in parts:
            if part not in seen:
                seen.add(part)
                rendered.append(part)
        if len("\n".join(rendered)) >= max_chars:
            break
    snippet = "\n".join(rendered).strip()
    return snippet[:max_chars].rstrip()


def _is_table_content_line(line: str) -> bool:
    return "|" in line and not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line)


def _text_has_table_header(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not _is_table_content_line(line):
            continue
        if index + 1 < len(lines) and _is_markdown_separator_line(lines[index + 1]):
            return True
    return False


def _nearest_previous_table_header(chunk: Chunk, document_chunks: list[Chunk]) -> str:
    previous = [candidate for candidate in document_chunks if int(candidate.ordinal or 0) < int(chunk.ordinal or 0)]
    for candidate in reversed(previous):
        header = _first_table_header_line(str(candidate.text or ""))
        if header:
            return header
    return ""


def _first_table_header_line(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not _is_table_content_line(line):
            continue
        has_separator = index + 1 < len(lines) and _is_markdown_separator_line(lines[index + 1])
        if has_separator:
            return line
    return ""


def _is_markdown_separator_line(line: str) -> bool:
    cells = [cell.strip() for cell in _table_cells(line) if cell.strip()]
    return len(cells) >= 2 and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells)


def _text_has_table_row_with_cell_count(text: str, cell_count: int) -> bool:
    if cell_count <= 0:
        return False
    for line in str(text or "").splitlines():
        line = line.strip()
        if _is_table_content_line(line) and len(_table_cells(line)) == cell_count:
            return True
    return False


def _cell_looks_data_value(cell: str) -> bool:
    value = str(cell or "").strip()
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", value) or re.fullmatch(r"[A-Z]{2,}-[A-Z0-9-]+", value))


def _line_has_anchor(line: str, anchors: list[str]) -> bool:
    folded = line.casefold()
    return any(anchor and anchor in folded for anchor in anchors)


def _focused_snippet_query_coverage(snippet: str, terms: list[str]) -> int:
    folded = str(snippet or "").casefold()
    return sum(1 for term in set(terms) if term.casefold() in folded)


def _anchor_coverage(anchors: list[str], text: str) -> float:
    normalized_text = _normalize_metric_phrase(text)
    if not anchors or not normalized_text:
        return 0.0
    normalized_anchors = [anchor for anchor in (_normalize_metric_phrase(anchor) for anchor in anchors) if anchor]
    if not normalized_anchors:
        return 0.0
    hits = sum(1 for anchor in dict.fromkeys(normalized_anchors) if anchor in normalized_text)
    return hits / len(set(normalized_anchors))


def _anchor_hit_count(anchors: list[str], text: str) -> int:
    normalized_text = _normalize_metric_phrase(text)
    if not anchors or not normalized_text:
        return 0
    normalized_anchors = [anchor for anchor in (_normalize_metric_phrase(anchor) for anchor in anchors) if anchor]
    return sum(1 for anchor in dict.fromkeys(normalized_anchors) if anchor in normalized_text)


def _query_seeks_numeric_answer(query: str) -> bool:
    folded = str(query or "").casefold()
    return bool(
        any(
            marker in folded
            for marker in (
                "amount",
                "count",
                "how many",
                "how much",
                "number",
                "percentage",
                "rate",
                "ratio",
                "value",
                "多少",
                "几",
                "数值",
                "金额",
                "百分比",
                "比例",
            )
        )
        or re.search(r"(?:是多少|为多少|有多少|几何|几%)", folded)
    )


def _text_has_numeric_evidence(text: str) -> bool:
    return bool(re.search(r"(?<!\w)[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?(?!\w)|(?<!\w)[+-]?\d+\.\d+%?(?!\w)", str(text or "")))


def _looks_like_tabular_evidence(text: str) -> bool:
    value = str(text or "")
    if "|" in value:
        return True
    numeric_rows = 0
    for line in value.splitlines() or [value]:
        if len(re.findall(r"(?<!\w)[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?(?!\w)|(?<!\w)[+-]?\d+\.\d+%?(?!\w)", line)) >= 3:
            numeric_rows += 1
    table_markers = r"单位|unit|quarter|季度|amount|value|metric|total|合计|项目|数值|金额|rate|ratio|比例"
    return numeric_rows > 0 and bool(re.search(table_markers, value, flags=re.IGNORECASE))


def _validation_table_penalty(snippet: str, query: str) -> float:
    if "|" not in str(snippet or ""):
        return 0.0
    query_folded = str(query or "").casefold()
    validation_intent_terms = {
        "actual",
        "check",
        "expected",
        "status",
        "validate",
        "validation",
        "verification",
        "verify",
        "实际",
        "校验",
        "核对",
        "验证",
        "预期",
        "状态",
    }
    if any(term in query_folded for term in validation_intent_terms):
        return 0.0
    for line in str(snippet or "").splitlines():
        if not _is_table_content_line(line):
            continue
        cells = [cell.casefold() for cell in _table_cells(line) if cell.strip()]
        if len(cells) < 3:
            continue
        has_expected = any("expected" in cell or "预期" in cell for cell in cells)
        has_actual = any("actual" in cell or "实际" in cell for cell in cells)
        has_status = any("status" in cell or "状态" in cell for cell in cells)
        has_check = any(
            any(token in cell for token in ("check", "validate", "validation", "verify", "校验", "核对", "验证"))
            for cell in cells
        )
        if has_expected and has_actual and (has_status or has_check):
            return 0.04
    return 0.0


def _line_anchor_score(line: str, anchors: list[str]) -> int:
    folded = line.casefold()
    score = 0
    for anchor in anchors:
        if not anchor or anchor not in folded:
            continue
        score += 4 if re.search(r"\d", anchor) or "-" in anchor or "_" in anchor else 1
    return score


def _table_header_index(lines: list[str], row_index: int) -> int | None:
    index = row_index
    while index > 0 and ("|" in lines[index - 1] or re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", lines[index - 1])):
        index -= 1
    for candidate in range(index, row_index + 1):
        if _is_table_content_line(lines[candidate]):
            return candidate
    return None


def _render_focused_table_row(header: str, row: str, anchors: list[str], *, max_chars: int) -> str:
    header_cells = _table_cells(header)
    row_cells = _table_cells(row)
    if not header_cells or len(header_cells) != len(row_cells):
        return "\n".join([header, row])
    if len(header_cells) <= 6:
        focused = "\n".join([header, row])
        if len(focused) <= max_chars:
            return focused
    selected: list[int] = []
    anchor_text = " ".join(anchors).casefold()
    for index, (header_cell, row_cell) in enumerate(zip(header_cells, row_cells)):
        cell_text = f"{header_cell} {row_cell}".casefold()
        if any(anchor in cell_text for anchor in anchors) or _table_header_matches_query_intent(header_cell, anchor_text):
            selected.append(index)
    for index in range(min(3, len(header_cells))):
        if index not in selected:
            selected.insert(index, index)
    selected = sorted(dict.fromkeys(selected))[:16]
    focused_header = _render_table_cells([header_cells[index] for index in selected])
    focused_row = _render_table_cells([row_cells[index] for index in selected])
    focused = f"{focused_header}\n{focused_row}"
    if len(focused) <= max_chars:
        return focused
    return focused[:max_chars].rstrip()


def _table_header_matches_query_intent(header: str, anchor_text: str) -> bool:
    folded = header.casefold()
    return (
        (folded in {"next step", "action", "next action"} and any(term in anchor_text for term in ("next", "step", "action", "下一步", "行动")))
        or (folded in {"lead", "owner", "responsible"} and any(term in anchor_text for term in ("lead", "owner", "负责人")))
        or (folded == "status" and any(term in anchor_text for term in ("status", "状态")))
        or (folded == "arr" and "arr" in anchor_text)
    )


def _table_cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _render_table_cells(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _best_anchor_index(text: str, anchors: list[str]) -> int:
    folded = text.casefold()
    for anchor in anchors:
        index = folded.find(anchor)
        if index >= 0:
            return index
    return -1


def _expand_snippet_to_line_boundaries(text: str, start: int, end: int, *, max_chars: int) -> tuple[int, int]:
    line_start = text.rfind("\n", 0, start)
    if line_start >= 0 and end - line_start <= max_chars:
        start = line_start + 1
    line_end = text.find("\n", end)
    if line_end >= 0 and line_end - start <= max_chars:
        end = line_end
    return start, end


def _bm25_scores(documents: list[list[str]], query_terms: list[str]) -> list[float] | None:
    if not documents or not query_terms:
        return None
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - optional retrieval dependency.
        return None
    try:
        return [float(score) for score in BM25Okapi(documents).get_scores(query_terms)]
    except Exception:  # noqa: BLE001 - malformed optional dependency should not break retrieval.
        return None


def _latest_source_time(items: list[SourceItem]) -> datetime | None:
    timestamps = [_source_time(item) for item in items]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(timestamps) if timestamps else None


def _primary_result_source(score_debug: dict[str, float]) -> str:
    if score_debug.get("scope", 0.0) > 0:
        return "scope"
    if score_debug.get("exact_source", 0.0) > 0:
        return "exact_source"
    if score_debug.get("exact_identifier", 0.0) > 0:
        return "exact_identifier"
    if score_debug.get("anchor_overlap", 0.0) > 0:
        return "anchor_overlap"
    if score_debug.get("vector_rank", 0.0) > 0 or score_debug.get("vector", 0.0) > 0:
        return "vector"
    if score_debug.get("graph_expansion", 0.0) > 0 or (
        score_debug.get("graph_ppr", 0.0) > 0 and score_debug.get("lexical", 0.0) <= 0
    ):
        return "graph"
    if score_debug.get("lexical_rank", 0.0) > 0 or score_debug.get("lexical", 0.0) > 0:
        return "lexical"
    return "unknown"


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


def _fuzzy_alias_match(haystack: str, alias: str) -> bool:
    if _contains_cjk(alias) or len(alias.replace(" ", "")) < 6:
        return False
    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - optional retrieval dependency.
        return False
    try:
        return float(fuzz.WRatio(alias, haystack)) >= 90.0
    except Exception:  # noqa: BLE001 - optional dependency should not break retrieval.
        return False


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


def _hipporag_fact_score(
    *,
    query_terms: list[str],
    edge: Hyperedge,
    members: list[HyperedgeMember],
    entity_by_id: dict[str, Entity],
) -> float:
    labels = [
        entity_by_id[member.entity_id].label
        for member in members
        if member.entity_id in entity_by_id
    ]
    fact_text = " ".join([*labels, edge.relation_type, edge.evidence_text])
    fact_terms = re.findall(r"[\w\u4e00-\u9fff]+", fact_text.lower())
    lexical = _term_overlap_score(query_terms, fact_terms)
    mentioned = 0.0
    normalized_fact = _normalize_match_text(fact_text)
    for term in query_terms:
        if _match_position(normalized_fact, _normalize_match_text(term)) is not None:
            mentioned += 1.0
    mention_coverage = mentioned / len(query_terms) if query_terms else 0.0
    relevance = lexical * 0.7 + mention_coverage * 0.3
    if relevance <= 0:
        return 0.0
    confidence = max(0.0, min(1.0, float(edge.confidence or 0.0)))
    grounded = 1.0 if edge.source_refs else 0.0
    return relevance * (0.7 + confidence * 0.2 + grounded * 0.1)


def _top_fact_edges(
    candidate_edges: list[tuple[Hyperedge, list[HyperedgeMember], float]],
    *,
    limit: int,
) -> list[tuple[Hyperedge, list[HyperedgeMember], float]]:
    return sorted(
        [item for item in candidate_edges if item[2] > 0],
        key=lambda item: (
            item[2],
            float(item[0].confidence or 0.0),
            item[0].hyperedge_id,
        ),
        reverse=True,
    )[:limit]


def _term_overlap_score(query_terms: list[str], text_terms: list[str]) -> float:
    if not query_terms or not text_terms:
        return 0.0
    text_set = set(text_terms)
    overlap = sum(1 for term in set(query_terms) if term in text_set)
    return overlap / math.sqrt(len(text_set))


def _adjacency_edge_count(adjacency: dict[str, dict[str, float]]) -> int:
    return sum(len(neighbors) for neighbors in adjacency.values()) // 2


def _personalized_pagerank(
    adjacency: dict[str, dict[str, float]],
    seeds: dict[str, float],
    *,
    damping: float = 0.85,
    iterations: int = 24,
) -> dict[str, float]:
    active_seeds = {node_id: weight for node_id, weight in seeds.items() if weight > 0 and node_id in adjacency}
    seed_total = sum(active_seeds.values())
    if seed_total <= 0:
        return {}
    personalization = {node_id: weight / seed_total for node_id, weight in active_seeds.items()}
    nodes = set(adjacency)
    for neighbors in adjacency.values():
        nodes.update(neighbors)
    if not nodes:
        return {}

    scores = {node_id: personalization.get(node_id, 0.0) for node_id in nodes}
    for _ in range(iterations):
        next_scores = {node_id: (1.0 - damping) * personalization.get(node_id, 0.0) for node_id in nodes}
        dangling_mass = 0.0
        for node_id in nodes:
            neighbors = adjacency.get(node_id) or {}
            score = scores.get(node_id, 0.0)
            if not neighbors:
                dangling_mass += score
                continue
            weight_total = sum(max(weight, 0.0) for weight in neighbors.values())
            if weight_total <= 0:
                dangling_mass += score
                continue
            for neighbor_id, weight in neighbors.items():
                next_scores[neighbor_id] = next_scores.get(neighbor_id, 0.0) + damping * score * max(weight, 0.0) / weight_total
        if dangling_mass:
            for seed_node_id, seed_weight in personalization.items():
                next_scores[seed_node_id] = next_scores.get(seed_node_id, 0.0) + damping * dangling_mass * seed_weight
        scores = next_scores
    return scores


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
