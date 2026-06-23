from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any

from pska_core.models import Chunk, Entity, Hyperedge, HyperedgeMember, KnowledgeClaim, SourceItem, SourceRef
from pska_core.serde import to_jsonable


@dataclass(slots=True)
class HippoRAGFact:
    fact_id: str
    hyperedge_id: str
    relation_type: str
    member_entity_ids: list[str]
    member_labels: list[str]
    evidence_text: str
    confidence: float
    source_refs: list[SourceRef] = field(default_factory=list)
    fact_kind: str = "hyperedge"
    object_id: str = ""

    @property
    def text(self) -> str:
        return " ".join([*self.member_labels, self.relation_type, self.evidence_text])


@dataclass(slots=True)
class HippoRAGEntityLink:
    entity_id: str
    label: str
    score: float
    score_debug: dict[str, float] = field(default_factory=dict)


class HippoRAGFactReranker:
    def rerank(
        self,
        query: str,
        scored_facts: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        return scored_facts[:limit]


@dataclass(slots=True)
class HippoRAGOfflineIndex:
    adjacency: dict[str, dict[str, float]]
    facts: list[HippoRAGFact]
    entity_by_id: dict[str, Entity]
    chunk_by_id: dict[str, Chunk]
    chunk_by_source_item_id: dict[str, list[Chunk]]
    entity_to_chunk_ids: dict[str, set[str]]
    fact_to_chunk_ids: dict[str, set[str]]
    graph_info: dict[str, Any]
    fact_embeddings: dict[str, list[float]] = field(default_factory=dict)
    entity_embeddings: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        entities: list[Entity],
        hyperedges: list[tuple[Hyperedge, list[HyperedgeMember]]],
        knowledge_claims: list[KnowledgeClaim] | None = None,
        chunks: list[Chunk],
        item_by_id: dict[str, SourceItem],
    ) -> "HippoRAGOfflineIndex":
        adjacency: dict[str, dict[str, float]] = {}
        entity_by_id = {entity.entity_id: entity for entity in entities}
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        chunk_by_source_item_id: dict[str, list[Chunk]] = {}
        entity_to_chunk_ids: dict[str, set[str]] = {}
        fact_to_chunk_ids: dict[str, set[str]] = {}
        facts: list[HippoRAGFact] = []

        def add_node(node_id: str) -> None:
            adjacency.setdefault(node_id, {})

        def add_edge(left: str, right: str, weight: float) -> None:
            if left == right or weight <= 0:
                return
            add_node(left)
            add_node(right)
            adjacency[left][right] = max(adjacency[left].get(right, 0.0), weight)
            adjacency[right][left] = max(adjacency[right].get(left, 0.0), weight)

        for chunk in chunks:
            chunk_by_source_item_id.setdefault(chunk.source_item_id, []).append(chunk)
            add_node(_chunk_node(chunk.chunk_id))

        for entity in entities:
            add_node(_entity_node(entity.entity_id))
            aliases = _entity_aliases(entity)
            for chunk in chunks:
                item = item_by_id.get(chunk.source_item_id)
                haystack = _normalize_match_text(f"{item.title if item else ''} {chunk.text}")
                if any(_match_position(haystack, _normalize_match_text(alias)) is not None for alias in aliases):
                    entity_to_chunk_ids.setdefault(entity.entity_id, set()).add(chunk.chunk_id)
                    add_edge(_entity_node(entity.entity_id), _chunk_node(chunk.chunk_id), 0.45)

        seen_edges: set[str] = set()
        for edge, members in hyperedges:
            if edge.hyperedge_id in seen_edges:
                continue
            seen_edges.add(edge.hyperedge_id)
            visible_members = [member for member in members if member.entity_id in entity_by_id]
            if len(visible_members) < 2:
                continue

            member_entity_ids = [member.entity_id for member in visible_members]
            member_labels = [entity_by_id[entity_id].label for entity_id in member_entity_ids]
            fact = HippoRAGFact(
                fact_id=_fact_node(edge.hyperedge_id),
                hyperedge_id=edge.hyperedge_id,
                relation_type=edge.relation_type,
                member_entity_ids=member_entity_ids,
                member_labels=member_labels,
                evidence_text=edge.evidence_text,
                confidence=max(0.0, min(1.0, float(edge.confidence or 0.0))),
                source_refs=list(edge.source_refs),
                fact_kind="hyperedge",
                object_id=edge.hyperedge_id,
            )
            facts.append(fact)
            add_node(fact.fact_id)

            fact_weight = 0.7 + fact.confidence * 0.2 + (0.1 if fact.source_refs else 0.0)
            for entity_id in member_entity_ids:
                add_edge(fact.fact_id, _entity_node(entity_id), fact_weight)

            entity_edge_weight = 0.35 + fact.confidence * 0.45 + (0.2 if fact.source_refs else 0.0)
            for left_index, left_entity_id in enumerate(member_entity_ids):
                for right_entity_id in member_entity_ids[left_index + 1 :]:
                    add_edge(_entity_node(left_entity_id), _entity_node(right_entity_id), entity_edge_weight)

            evidence_chunk_ids = _chunks_for_source_refs(fact.source_refs, chunk_by_source_item_id, chunk_by_id)
            fact_to_chunk_ids[fact.fact_id] = set(evidence_chunk_ids)
            chunk_weight = 0.55 + fact.confidence * 0.35
            for chunk_id in evidence_chunk_ids:
                add_edge(fact.fact_id, _chunk_node(chunk_id), chunk_weight)
                for entity_id in member_entity_ids:
                    entity_to_chunk_ids.setdefault(entity_id, set()).add(chunk_id)
                    add_edge(_entity_node(entity_id), _chunk_node(chunk_id), chunk_weight)

        for claim in knowledge_claims or []:
            member_entity_ids = _claim_entity_ids(claim, entity_by_id)
            member_labels = [entity_by_id[entity_id].label for entity_id in member_entity_ids]
            fact = HippoRAGFact(
                fact_id=_claim_fact_node(claim.knowledge_claim_id),
                hyperedge_id="",
                relation_type=claim.predicate or claim.claim_type,
                member_entity_ids=member_entity_ids,
                member_labels=member_labels,
                evidence_text=" ".join(part for part in [claim.statement, claim.evidence_text] if part),
                confidence=max(0.0, min(1.0, float(claim.confidence or 0.0))),
                source_refs=list(claim.source_refs),
                fact_kind="knowledge_claim",
                object_id=claim.knowledge_claim_id,
            )
            facts.append(fact)
            add_node(fact.fact_id)
            claim_weight = 0.55 + fact.confidence * 0.25 + (0.1 if fact.source_refs else 0.0)
            for entity_id in member_entity_ids:
                add_edge(fact.fact_id, _entity_node(entity_id), claim_weight)
            evidence_chunk_ids = _chunks_for_source_refs(fact.source_refs, chunk_by_source_item_id, chunk_by_id)
            fact_to_chunk_ids[fact.fact_id] = set(evidence_chunk_ids)
            for chunk_id in evidence_chunk_ids:
                add_edge(fact.fact_id, _chunk_node(chunk_id), 0.55 + fact.confidence * 0.25)

        graph_info = {
            "num_phrase_nodes": len(entity_by_id),
            "num_passage_nodes": len(chunk_by_id),
            "num_fact_nodes": len(facts),
            "num_total_nodes": len(adjacency),
            "num_total_edges": _adjacency_edge_count(adjacency),
            "num_fact_to_entity_edges": sum(len(fact.member_entity_ids) for fact in facts),
            "num_fact_to_passage_edges": sum(len(chunk_ids) for chunk_ids in fact_to_chunk_ids.values()),
        }
        return cls(
            adjacency=adjacency,
            facts=facts,
            entity_by_id=entity_by_id,
            chunk_by_id=chunk_by_id,
            chunk_by_source_item_id=chunk_by_source_item_id,
            entity_to_chunk_ids=entity_to_chunk_ids,
            fact_to_chunk_ids=fact_to_chunk_ids,
            graph_info=graph_info,
        )

    def with_embeddings(self, embedding_provider) -> "HippoRAGOfflineIndex":
        if embedding_provider is None:
            return self
        fact_texts = [_fact_embedding_text(fact) for fact in self.facts]
        entity_items = sorted(self.entity_by_id.values(), key=lambda entity: entity.entity_id)
        entity_texts = [entity.label for entity in entity_items]
        fact_vectors = embedding_provider.embed_texts(fact_texts) if fact_texts else []
        entity_vectors = embedding_provider.embed_texts(entity_texts) if entity_texts else []
        self.fact_embeddings = {
            fact.fact_id: vector
            for fact, vector in zip(self.facts, fact_vectors)
        }
        self.entity_embeddings = {
            entity.entity_id: vector
            for entity, vector in zip(entity_items, entity_vectors)
        }
        self.graph_info["fact_embeddings"] = len(self.fact_embeddings)
        self.graph_info["entity_embeddings"] = len(self.entity_embeddings)
        return self

    def score_facts(
        self,
        query: str,
        *,
        limit: int = 8,
        query_embedding: list[float] | None = None,
        reranker: HippoRAGFactReranker | None = None,
    ) -> list[dict[str, Any]]:
        query_terms = _terms(query)
        scored = []
        embedding_only_count = 0
        for fact in self.facts:
            lexical_score = _fact_score(query_terms=query_terms, fact=fact)
            embedding_score = _cosine_similarity(query_embedding, self.fact_embeddings.get(fact.fact_id)) if query_embedding else 0.0
            score = _merge_relevance_scores(lexical_score=lexical_score, embedding_score=embedding_score)
            if score <= 0:
                continue
            if lexical_score <= 0 and embedding_score >= 0.8:
                embedding_only_count += 1
            scored.append(
                {
                    "fact": fact,
                    "score": score,
                    "score_debug": {
                        "lexical": lexical_score,
                        "embedding": embedding_score,
                    },
                    "summary": {
                        "hyperedge_id": fact.hyperedge_id,
                        "fact_kind": fact.fact_kind,
                        "object_id": fact.object_id,
                        "relation_type": fact.relation_type,
                        "score": round(score, 8),
                        "members": list(fact.member_labels),
                        "source_refs": to_jsonable(fact.source_refs),
                    },
                }
            )
        if embedding_only_count > 3:
            scored = [item for item in scored if item["score_debug"]["lexical"] > 0]
        ranked = sorted(
            scored,
            key=lambda item: (
                float(item["score"]),
                float(item["fact"].confidence),
                str(item["fact"].hyperedge_id),
            ),
            reverse=True,
        )
        reranked = (reranker or HippoRAGFactReranker()).rerank(query, ranked, limit=limit)
        return reranked[:limit]

    def link_entities(
        self,
        query: str,
        *,
        limit: int = 8,
        query_embedding: list[float] | None = None,
    ) -> list[HippoRAGEntityLink]:
        query_terms = _terms(query)
        links = []
        embedding_only_count = 0
        for entity in self.entity_by_id.values():
            lexical = _entity_score(query_terms=query_terms, entity=entity)
            embedding = _cosine_similarity(query_embedding, self.entity_embeddings.get(entity.entity_id)) if query_embedding else 0.0
            score = _merge_relevance_scores(lexical_score=lexical, embedding_score=embedding)
            if score <= 0:
                continue
            if lexical <= 0 and embedding >= 0.8:
                embedding_only_count += 1
            links.append(
                HippoRAGEntityLink(
                    entity_id=entity.entity_id,
                    label=entity.label,
                    score=score,
                    score_debug={
                        "lexical": lexical,
                        "embedding": embedding,
                    },
                )
            )
        if embedding_only_count > 3:
            links = [link for link in links if link.score_debug["lexical"] > 0]
        return sorted(links, key=lambda link: (link.score, link.entity_id), reverse=True)[:limit]


def _chunks_for_source_refs(
    source_refs: list[SourceRef],
    chunk_by_source_item_id: dict[str, list[Chunk]],
    chunk_by_id: dict[str, Chunk],
) -> list[str]:
    chunk_ids: list[str] = []
    seen: set[str] = set()
    for ref in source_refs:
        chunks = []
        if ref.chunk_id and ref.chunk_id in chunk_by_id:
            chunks = [chunk_by_id[ref.chunk_id]]
        elif ref.source_item_id:
            chunks = chunk_by_source_item_id.get(ref.source_item_id, [])[:4]
        for chunk in chunks:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            chunk_ids.append(chunk.chunk_id)
    return chunk_ids


def _fact_score(*, query_terms: list[str], fact: HippoRAGFact) -> float:
    fact_terms = _terms(fact.text)
    lexical = _term_overlap_score(query_terms, fact_terms)
    normalized_fact = _normalize_match_text(fact.text)
    mentioned = sum(
        1.0
        for term in query_terms
        if _match_position(normalized_fact, _normalize_match_text(term)) is not None
    )
    mention_coverage = mentioned / len(query_terms) if query_terms else 0.0
    relevance = lexical * 0.7 + mention_coverage * 0.3
    if relevance <= 0:
        return 0.0
    grounded = 1.0 if fact.source_refs else 0.0
    return relevance * (0.7 + fact.confidence * 0.2 + grounded * 0.1)


def _fact_embedding_text(fact: HippoRAGFact) -> str:
    return " ".join([*fact.member_labels, fact.relation_type])


def _entity_score(*, query_terms: list[str], entity: Entity) -> float:
    aliases = _entity_aliases(entity)
    text_terms = _terms(" ".join(aliases))
    lexical = _term_overlap_score(query_terms, text_terms)
    normalized_aliases = [_normalize_match_text(alias) for alias in aliases]
    mentioned = sum(
        1.0
        for term in query_terms
        if any(_match_position(alias, _normalize_match_text(term)) is not None for alias in normalized_aliases)
    )
    mention_coverage = mentioned / len(query_terms) if query_terms else 0.0
    return lexical * 0.7 + mention_coverage * 0.3


def _merge_relevance_scores(*, lexical_score: float, embedding_score: float) -> float:
    strong_embedding_score = embedding_score if embedding_score >= 0.8 else 0.0
    if strong_embedding_score <= 0:
        return lexical_score
    if lexical_score <= 0:
        return strong_embedding_score * 0.85
    return lexical_score * 0.45 + strong_embedding_score * 0.55


def _cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(0.0, dot / (left_norm * right_norm))


def _entity_node(entity_id: str) -> str:
    return f"entity:{entity_id}"


def _chunk_node(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def _fact_node(hyperedge_id: str) -> str:
    return f"fact:{hyperedge_id}"


def _claim_fact_node(knowledge_claim_id: str) -> str:
    return f"fact:claim:{knowledge_claim_id}"


def _claim_entity_ids(claim: KnowledgeClaim, entity_by_id: dict[str, Entity]) -> list[str]:
    labels = {
        _normalize_match_text(value)
        for value in [claim.subject, claim.object]
        if isinstance(value, str) and value.strip()
    }
    if not labels:
        return []
    result = []
    for entity in entity_by_id.values():
        aliases = {_normalize_match_text(alias) for alias in _entity_aliases(entity)}
        if labels & aliases:
            result.append(entity.entity_id)
    return result


def _terms(text: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff]+", text.lower())


def _term_overlap_score(query_terms: list[str], text_terms: list[str]) -> float:
    if not query_terms or not text_terms:
        return 0.0
    text_set = set(text_terms)
    overlap = sum(1 for term in set(query_terms) if term in text_set)
    return overlap / math.sqrt(len(text_set))


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
    return list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip()))


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


def _adjacency_edge_count(adjacency: dict[str, dict[str, float]]) -> int:
    return sum(len(neighbors) for neighbors in adjacency.values()) // 2
