from __future__ import annotations

from hashlib import sha256
from typing import Any, Protocol

from pska_core.enums import ReviewType
from pska_core.models import DEFAULT_TENANT_ID, DiscoveryItem, ReviewItem, SourceItem
from pska_core.serde import to_jsonable

DISCOVERY_TODAY_SCORE_THRESHOLD = 0.5


class DiscoveryProducer(Protocol):
    producer_name: str

    def produce(self) -> list[DiscoveryItem]: ...


class RelationshipDiscoveryProducer:
    producer_name = "RelationshipDiscoveryProducer"

    def __init__(self, store, *, owner_user_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store
        self.owner_user_id = owner_user_id
        self.tenant_id = tenant_id

    def produce(self) -> list[DiscoveryItem]:
        return [
            _review_discovery(
                item,
                discovery_type="relationship",
                producer=self.producer_name,
                fallback_title="New relationship candidate",
            )
            for item in self.store.list_review_items(tenant_id=self.tenant_id)
            if item.owner_user_id == self.owner_user_id
            and item.status == "pending"
            and item.review_type == ReviewType.RELATIONSHIP_CANDIDATE
        ]


class ConflictDiscoveryProducer:
    producer_name = "ConflictDiscoveryProducer"

    def __init__(self, store, *, owner_user_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store
        self.owner_user_id = owner_user_id
        self.tenant_id = tenant_id

    def produce(self) -> list[DiscoveryItem]:
        return [
            _review_discovery(
                item,
                discovery_type="conflict",
                producer=self.producer_name,
                fallback_title="Possible conflict",
            )
            for item in self.store.list_review_items(tenant_id=self.tenant_id)
            if item.owner_user_id == self.owner_user_id
            and item.status == "pending"
            and item.review_type == ReviewType.CONFLICT
        ]


class MemoryDiscoveryProducer:
    producer_name = "MemoryDiscoveryProducer"

    def __init__(self, store, *, owner_user_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store
        self.owner_user_id = owner_user_id
        self.tenant_id = tenant_id

    def produce(self) -> list[DiscoveryItem]:
        results = []
        for item in self.store.list_review_items(tenant_id=self.tenant_id):
            if item.owner_user_id != self.owner_user_id or item.status != "pending":
                continue
            if item.review_type not in {ReviewType.MEMORY_CANDIDATE, ReviewType.LOW_CONFIDENCE, ReviewType.PROFILE_UPDATE}:
                continue
            if not _memory_text(item):
                continue
            results.append(
                _review_discovery(
                    item,
                    discovery_type="memory",
                    producer=self.producer_name,
                    fallback_title="New memory candidate",
                )
            )
        return results


class TopicDiscoveryProducer:
    producer_name = "TopicDiscoveryProducer"

    def __init__(self, store, *, owner_user_id: str, tenant_id: str = DEFAULT_TENANT_ID, limit: int = 8) -> None:
        self.store = store
        self.owner_user_id = owner_user_id
        self.tenant_id = tenant_id
        self.limit = limit

    def produce(self) -> list[DiscoveryItem]:
        results = []
        for item in sorted(self.store.list_source_items(tenant_id=self.tenant_id), key=lambda source: source.created_at, reverse=True):
            if item.owner_user_id != self.owner_user_id:
                continue
            title = item.title.strip()
            if not title:
                continue
            results.append(_topic_discovery(item, producer=self.producer_name))
            if len(results) >= self.limit:
                break
        return results


class DiscoveryService:
    def __init__(self, store, *, owner_user_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store
        self.owner_user_id = owner_user_id
        self.tenant_id = tenant_id
        self.producers: list[DiscoveryProducer] = [
            RelationshipDiscoveryProducer(store, owner_user_id=owner_user_id, tenant_id=tenant_id),
            ConflictDiscoveryProducer(store, owner_user_id=owner_user_id, tenant_id=tenant_id),
            MemoryDiscoveryProducer(store, owner_user_id=owner_user_id, tenant_id=tenant_id),
            TopicDiscoveryProducer(store, owner_user_id=owner_user_id, tenant_id=tenant_id),
        ]

    def produce(self) -> list[DiscoveryItem]:
        produced: list[DiscoveryItem] = []
        for producer in self.producers:
            for item in producer.produce():
                produced.append(self.store.upsert_discovery_item(item))
        return produced


def _review_discovery(item: ReviewItem, *, discovery_type: str, producer: str, fallback_title: str) -> DiscoveryItem:
    proposal = item.proposal or {}
    confidence = float(proposal.get("confidence") or 0.75)
    title = item.title or fallback_title
    evidence = [
        {
            "kind": "review_item",
            "review_item_id": item.review_item_id,
            "review_type": item.review_type.value if hasattr(item.review_type, "value") else str(item.review_type),
            "source_refs": to_jsonable(proposal.get("source_refs") or []),
            "proposal": to_jsonable(proposal),
            "created_at": item.created_at.isoformat(),
        }
    ]
    fingerprint = _fingerprint(producer, discovery_type, title, _review_semantic_key(item))
    score, signals = DiscoveryScorer().score(
        discovery_type=discovery_type,
        title=title,
        evidence=evidence,
        confidence=confidence,
        producer=producer,
    )
    return DiscoveryItem(
        discovery_id=_discovery_id(producer, item.review_item_id),
        owner_user_id=item.owner_user_id,
        tenant_id=item.tenant_id,
        discovery_type=discovery_type,
        title=title,
        evidence=evidence,
        confidence=max(0.0, min(confidence, 1.0)),
        producer=producer,
        fingerprint=fingerprint,
        evidence_snapshot=evidence,
        discovery_score=score,
        quality_signals=signals,
        status="new",
    )


def _topic_discovery(item: SourceItem, *, producer: str) -> DiscoveryItem:
    title = f"New topic: {item.title}"
    evidence = [
        {
            "kind": "source_item",
            "source_item_id": item.source_item_id,
            "source_channel": item.source_channel,
            "record_type": item.record_type,
            "title": item.title,
            "url": item.url,
            "created_at": item.created_at.isoformat(),
        }
    ]
    fingerprint = _fingerprint(producer, "topic", item.title, item.source_channel)
    score, signals = DiscoveryScorer().score(
        discovery_type="topic",
        title=title,
        evidence=evidence,
        confidence=0.62,
        producer=producer,
    )
    return DiscoveryItem(
        discovery_id=_discovery_id(producer, item.source_item_id),
        owner_user_id=item.owner_user_id,
        tenant_id=item.tenant_id,
        discovery_type="topic",
        title=title,
        evidence=evidence,
        confidence=0.62,
        producer=producer,
        fingerprint=fingerprint,
        evidence_snapshot=evidence,
        discovery_score=score,
        quality_signals=signals,
        status="new",
    )


class DiscoveryScorer:
    def score(
        self,
        *,
        discovery_type: str,
        title: str,
        evidence: list[dict[str, Any]],
        confidence: float,
        producer: str,
    ) -> tuple[float, dict[str, Any]]:
        evidence_count = len(evidence)
        unique_sources = _unique_evidence_sources(evidence)
        novelty = _novelty_signal(title, discovery_type)
        cross_source = 1.0 if unique_sources >= 3 else 0.75 if unique_sources == 2 else 0.25 if unique_sources == 1 else 0.0
        temporal_span = _temporal_span_signal(evidence)
        evidence_strength = min(1.0, evidence_count / 3.0)
        graph_impact = _graph_impact_signal(discovery_type)
        review_likelihood = max(0.0, min(float(confidence or 0.0), 1.0))
        governance_signal = 1.0 if producer != TopicDiscoveryProducer.producer_name else 0.0
        producer_penalty = 0.2 if producer == TopicDiscoveryProducer.producer_name and title.lower().startswith("new topic:") else 0.0

        weighted = (
            novelty * 0.22
            + cross_source * 0.16
            + temporal_span * 0.10
            + evidence_strength * 0.13
            + graph_impact * 0.14
            + review_likelihood * 0.13
            + governance_signal * 0.12
            - producer_penalty
        )
        if discovery_type == "topic" and any(item.get("kind") == "source_item" for item in evidence):
            weighted = max(weighted, 0.52)
        score = round(max(0.0, min(weighted, 1.0)), 3)
        signals = {
            "novelty": round(novelty, 3),
            "cross_source": round(cross_source, 3),
            "temporal_span": round(temporal_span, 3),
            "evidence_strength": round(evidence_strength, 3),
            "graph_impact": round(graph_impact, 3),
            "review_likelihood": round(review_likelihood, 3),
            "governance_signal": round(governance_signal, 3),
            "producer_penalty": round(producer_penalty, 3),
            "source_topic_floor": 0.52 if discovery_type == "topic" and any(item.get("kind") == "source_item" for item in evidence) else 0.0,
            "evidence_count": evidence_count,
            "unique_sources": unique_sources,
            "score_threshold": DISCOVERY_TODAY_SCORE_THRESHOLD,
        }
        return score, signals


def _memory_text(item: ReviewItem) -> str:
    proposal = item.proposal or {}
    if proposal.get("memory_candidate") or proposal.get("text"):
        return str(proposal.get("memory_candidate") or proposal.get("text") or "").strip()
    profile_delta = proposal.get("profile_delta") if isinstance(proposal.get("profile_delta"), dict) else {}
    return str(profile_delta.get("memory_candidate") or "").strip()


def _discovery_id(producer: str, fingerprint: str) -> str:
    digest = sha256(f"{producer}:{fingerprint}".encode("utf-8")).hexdigest()[:32]
    return f"disc_{digest}"


def _fingerprint(*parts: str) -> str:
    normalized = ":".join(_normalize_text(part) for part in parts if part)
    return sha256(normalized.encode("utf-8")).hexdigest()


def _review_semantic_key(item: ReviewItem) -> str:
    proposal = item.proposal or {}
    if item.review_type == ReviewType.RELATIONSHIP_CANDIDATE:
        members = proposal.get("members") or proposal.get("entities") or proposal.get("entity_ids") or []
        relation = proposal.get("relation_type") or proposal.get("relationship") or proposal.get("predicate") or item.title
        return f"{relation}:{to_jsonable(members)}"
    if item.review_type == ReviewType.CONFLICT:
        return f"{item.title}:{to_jsonable(proposal.get('claims') or proposal.get('conflict') or proposal)}"
    if item.review_type in {ReviewType.MEMORY_CANDIDATE, ReviewType.LOW_CONFIDENCE, ReviewType.PROFILE_UPDATE}:
        return _memory_text(item) or item.title
    return item.review_item_id


def _unique_evidence_sources(evidence: list[dict[str, Any]]) -> int:
    sources = set()
    for item in evidence:
        if item.get("source_item_id"):
            sources.add(str(item["source_item_id"]))
        for ref in item.get("source_refs") or []:
            if isinstance(ref, dict) and (ref.get("source_item_id") or ref.get("chunk_id")):
                sources.add(str(ref.get("source_item_id") or ref.get("chunk_id")))
    return len(sources)


def _temporal_span_signal(evidence: list[dict[str, Any]]) -> float:
    dates = []
    for item in evidence:
        value = item.get("created_at")
        if not isinstance(value, str):
            continue
        try:
            dates.append(value[:10])
        except ValueError:
            continue
    return 0.6 if len(set(dates)) >= 2 else 0.0


def _novelty_signal(title: str, discovery_type: str) -> float:
    normalized = _normalize_text(title)
    generic_terms = {"agent", "knowledge", "system", "topic", "note", "mock", "smoke", "canary"}
    if any(term in normalized for term in generic_terms):
        base = 0.25
    elif discovery_type in {"relationship", "conflict", "memory"}:
        base = 0.8
    else:
        base = 0.55
    if normalized.startswith("new topic"):
        base -= 0.2
    return max(0.0, min(base, 1.0))


def _graph_impact_signal(discovery_type: str) -> float:
    if discovery_type == "relationship":
        return 0.85
    if discovery_type == "conflict":
        return 0.8
    if discovery_type == "memory":
        return 0.65
    return 0.25


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())
