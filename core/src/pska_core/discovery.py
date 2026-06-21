from __future__ import annotations

from hashlib import sha256
from typing import Protocol

from pska_core.enums import ReviewType
from pska_core.models import DiscoveryItem, ReviewItem, SourceItem
from pska_core.serde import to_jsonable


class DiscoveryProducer(Protocol):
    producer_name: str

    def produce(self) -> list[DiscoveryItem]: ...


class RelationshipDiscoveryProducer:
    producer_name = "RelationshipDiscoveryProducer"

    def __init__(self, store, *, owner_user_id: str) -> None:
        self.store = store
        self.owner_user_id = owner_user_id

    def produce(self) -> list[DiscoveryItem]:
        return [
            _review_discovery(
                item,
                discovery_type="relationship",
                producer=self.producer_name,
                fallback_title="New relationship candidate",
            )
            for item in self.store.list_review_items()
            if item.owner_user_id == self.owner_user_id
            and item.status == "pending"
            and item.review_type == ReviewType.RELATIONSHIP_CANDIDATE
        ]


class ConflictDiscoveryProducer:
    producer_name = "ConflictDiscoveryProducer"

    def __init__(self, store, *, owner_user_id: str) -> None:
        self.store = store
        self.owner_user_id = owner_user_id

    def produce(self) -> list[DiscoveryItem]:
        return [
            _review_discovery(
                item,
                discovery_type="conflict",
                producer=self.producer_name,
                fallback_title="Possible conflict",
            )
            for item in self.store.list_review_items()
            if item.owner_user_id == self.owner_user_id
            and item.status == "pending"
            and item.review_type == ReviewType.CONFLICT
        ]


class MemoryDiscoveryProducer:
    producer_name = "MemoryDiscoveryProducer"

    def __init__(self, store, *, owner_user_id: str) -> None:
        self.store = store
        self.owner_user_id = owner_user_id

    def produce(self) -> list[DiscoveryItem]:
        results = []
        for item in self.store.list_review_items():
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

    def __init__(self, store, *, owner_user_id: str, limit: int = 8) -> None:
        self.store = store
        self.owner_user_id = owner_user_id
        self.limit = limit

    def produce(self) -> list[DiscoveryItem]:
        results = []
        for item in sorted(self.store.list_source_items(), key=lambda source: source.created_at, reverse=True):
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
    def __init__(self, store, *, owner_user_id: str) -> None:
        self.store = store
        self.owner_user_id = owner_user_id
        self.producers: list[DiscoveryProducer] = [
            RelationshipDiscoveryProducer(store, owner_user_id=owner_user_id),
            ConflictDiscoveryProducer(store, owner_user_id=owner_user_id),
            MemoryDiscoveryProducer(store, owner_user_id=owner_user_id),
            TopicDiscoveryProducer(store, owner_user_id=owner_user_id),
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
    return DiscoveryItem(
        discovery_id=_discovery_id(producer, item.review_item_id),
        owner_user_id=item.owner_user_id,
        discovery_type=discovery_type,
        title=item.title or fallback_title,
        evidence=[
            {
                "kind": "review_item",
                "review_item_id": item.review_item_id,
                "review_type": item.review_type.value if hasattr(item.review_type, "value") else str(item.review_type),
                "source_refs": to_jsonable(proposal.get("source_refs") or []),
                "proposal": to_jsonable(proposal),
            }
        ],
        confidence=max(0.0, min(confidence, 1.0)),
        producer=producer,
        status="new",
    )


def _topic_discovery(item: SourceItem, *, producer: str) -> DiscoveryItem:
    return DiscoveryItem(
        discovery_id=_discovery_id(producer, item.source_item_id),
        owner_user_id=item.owner_user_id,
        discovery_type="topic",
        title=f"New topic: {item.title}",
        evidence=[
            {
                "kind": "source_item",
                "source_item_id": item.source_item_id,
                "source_channel": item.source_channel,
                "record_type": item.record_type,
                "title": item.title,
                "url": item.url,
            }
        ],
        confidence=0.62,
        producer=producer,
        status="new",
    )


def _memory_text(item: ReviewItem) -> str:
    proposal = item.proposal or {}
    if proposal.get("memory_candidate") or proposal.get("text"):
        return str(proposal.get("memory_candidate") or proposal.get("text") or "").strip()
    profile_delta = proposal.get("profile_delta") if isinstance(proposal.get("profile_delta"), dict) else {}
    return str(profile_delta.get("memory_candidate") or "").strip()


def _discovery_id(producer: str, source_id: str) -> str:
    digest = sha256(f"{producer}:{source_id}".encode("utf-8")).hexdigest()[:32]
    return f"disc_{digest}"
