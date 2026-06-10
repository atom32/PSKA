from __future__ import annotations

from dataclasses import asdict
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
