from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from pska_core.enums import MemoryLayer, ReviewType
from pska_core.models import AgentMemory, ReviewItem, SourceRef, UserProfileCard
from pska_core.store import KnowledgeStore


class MemoryService:
    """User-owned agent memory and profile-card proposal management."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def write_agent_memory(
        self,
        *,
        owner_user_id: str,
        layer: MemoryLayer,
        text: str,
        confidence: float,
        source_refs: list[SourceRef],
        created_by_user_id: str | None = None,
        decay_policy: str = "manual",
    ) -> AgentMemory:
        memory = AgentMemory(
            agent_memory_id=f"agm_{uuid4().hex}",
            owner_user_id=owner_user_id,
            layer=layer,
            text=text,
            confidence=confidence,
            source_refs=source_refs,
            decay_policy=decay_policy,
            created_by_user_id=created_by_user_id,
        )
        self.store.add_agent_memory(memory)
        return memory

    def propose_profile_update(
        self,
        *,
        owner_user_id: str,
        profile_delta: dict,
        source_refs: list[SourceRef],
        sensitivity: str = "normal",
    ) -> UserProfileCard | ReviewItem:
        if sensitivity in {"high", "sensitive"}:
            review = ReviewItem(
                review_item_id=f"rev_{uuid4().hex}",
                owner_user_id=owner_user_id,
                review_type=ReviewType.PROFILE_UPDATE,
                title="Profile card update requires review",
                proposal={"profile_delta": profile_delta, "source_refs": [asdict(ref) for ref in source_refs]},
            )
            self.store.add_review_item(review)
            return review
        card = UserProfileCard(
            profile_card_id=f"upc_{uuid4().hex}",
            owner_user_id=owner_user_id,
            profile=profile_delta,
            source_refs=source_refs,
            confidence=0.8,
        )
        self.store.add_profile_card(card)
        return card

    def verify_agent_memory(
        self,
        agent_memory_id: str,
        *,
        confidence: float,
        decay_policy: str | None = None,
        verified_at: datetime | None = None,
    ) -> AgentMemory:
        memory = self.store.get_agent_memory(agent_memory_id)
        return self.store.update_agent_memory_lifecycle(
            agent_memory_id,
            confidence=_confidence(confidence),
            decay_policy=decay_policy or memory.decay_policy,
            last_verified_at=verified_at or datetime.now(timezone.utc),
        )

    def forget_agent_memory(self, agent_memory_id: str) -> AgentMemory:
        return self.store.update_agent_memory_lifecycle(
            agent_memory_id,
            confidence=0.0,
            decay_policy="forgotten",
            last_verified_at=datetime.now(timezone.utc),
        )


def _confidence(value: float) -> float:
    if value < 0 or value > 1:
        raise ValueError("confidence must be between 0 and 1")
    return float(value)
