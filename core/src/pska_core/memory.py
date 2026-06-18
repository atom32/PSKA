from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
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
        memory, _metadata = self.promote_agent_memory(
            owner_user_id=owner_user_id,
            layer=layer,
            text=text,
            confidence=confidence,
            source_refs=source_refs,
            created_by_user_id=created_by_user_id,
            decay_policy=decay_policy,
        )
        return memory

    def promote_agent_memory(
        self,
        *,
        owner_user_id: str,
        layer: MemoryLayer,
        text: str,
        confidence: float,
        source_refs: list[SourceRef],
        created_by_user_id: str | None = None,
        decay_policy: str = "manual",
        verified_at: datetime | None = None,
    ) -> tuple[AgentMemory, dict]:
        confidence = _confidence(confidence)
        layer = MemoryLayer(layer)
        text = text.strip()
        if not text:
            raise ValueError("agent memory text is required")
        verified_at = verified_at or datetime.now(timezone.utc)
        existing = self._find_agent_memory(owner_user_id=owner_user_id, layer=layer, text=text)
        if existing is not None:
            existing_ref_count = len(existing.source_refs)
            merged_refs = _merge_source_refs(existing.source_refs, source_refs)
            updated = self.store.update_agent_memory_lifecycle(
                existing.agent_memory_id,
                confidence=max(existing.confidence, confidence),
                decay_policy=decay_policy or existing.decay_policy,
                last_verified_at=verified_at,
                source_refs=merged_refs,
            )
            return updated, {
                "action": "updated",
                "agent_memory_id": updated.agent_memory_id,
                "source_refs_merged": len(merged_refs) - existing_ref_count,
                "confidence": updated.confidence,
                "last_verified_at": updated.last_verified_at,
            }
        memory = AgentMemory(
            agent_memory_id=f"agm_{uuid4().hex}",
            owner_user_id=owner_user_id,
            layer=layer,
            text=text,
            confidence=confidence,
            source_refs=source_refs,
            decay_policy=decay_policy,
            last_verified_at=verified_at,
            created_by_user_id=created_by_user_id,
        )
        self.store.add_agent_memory(memory)
        return memory, {
            "action": "created",
            "agent_memory_id": memory.agent_memory_id,
            "source_refs_merged": len(source_refs),
            "confidence": memory.confidence,
            "last_verified_at": memory.last_verified_at,
        }

    def propose_profile_update(
        self,
        *,
        owner_user_id: str,
        profile_delta: dict,
        source_refs: list[SourceRef],
        sensitivity: str = "normal",
        confidence: float = 0.8,
    ) -> UserProfileCard | ReviewItem:
        confidence = _confidence(confidence)
        if sensitivity in {"high", "sensitive"}:
            review = ReviewItem(
                review_item_id=f"rev_{uuid4().hex}",
                owner_user_id=owner_user_id,
                review_type=ReviewType.PROFILE_UPDATE,
                title="Profile card update requires review",
                proposal={
                    "profile_delta": profile_delta,
                    "source_refs": [asdict(ref) for ref in source_refs],
                    "confidence": confidence,
                },
            )
            self.store.add_review_item(review)
            return review
        card, _metadata = self.promote_profile_card(
            owner_user_id=owner_user_id,
            profile=profile_delta,
            source_refs=source_refs,
            confidence=confidence,
        )
        return card

    def promote_profile_card(
        self,
        *,
        owner_user_id: str,
        profile: dict,
        source_refs: list[SourceRef],
        confidence: float = 0.8,
        verified_at: datetime | None = None,
    ) -> tuple[UserProfileCard, dict]:
        confidence = _confidence(confidence)
        if not isinstance(profile, dict) or not profile:
            raise ValueError("profile must be a non-empty dict")
        verified_at = verified_at or datetime.now(timezone.utc)
        existing = self._find_profile_card(owner_user_id=owner_user_id, profile=profile)
        if existing is not None:
            existing_ref_count = len(existing.source_refs)
            merged_refs = _merge_source_refs(existing.source_refs, source_refs)
            updated = self.store.update_profile_card_lifecycle(
                existing.profile_card_id,
                confidence=max(existing.confidence, confidence),
                source_refs=merged_refs,
                last_verified_at=verified_at,
            )
            return updated, {
                "action": "updated",
                "profile_card_id": updated.profile_card_id,
                "source_refs_merged": len(merged_refs) - existing_ref_count,
                "confidence": updated.confidence,
                "last_verified_at": updated.last_verified_at,
            }
        card = UserProfileCard(
            profile_card_id=f"upc_{uuid4().hex}",
            owner_user_id=owner_user_id,
            profile=profile,
            source_refs=source_refs,
            confidence=confidence,
            last_verified_at=verified_at,
        )
        self.store.add_profile_card(card)
        return card, {
            "action": "created",
            "profile_card_id": card.profile_card_id,
            "source_refs_merged": len(source_refs),
            "confidence": card.confidence,
            "last_verified_at": card.last_verified_at,
        }

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

    def _find_agent_memory(self, *, owner_user_id: str, layer: MemoryLayer, text: str) -> AgentMemory | None:
        normalized = _normalize_text(text)
        for memory in self.store.list_agent_memories(owner_user_id=owner_user_id):
            if MemoryLayer(memory.layer) == layer and _normalize_text(memory.text) == normalized:
                return memory
        return None

    def _find_profile_card(self, *, owner_user_id: str, profile: dict) -> UserProfileCard | None:
        key = _profile_key(profile)
        for card in self.store.list_profile_cards(owner_user_id=owner_user_id):
            if _profile_key(card.profile) == key:
                return card
        return None


def _confidence(value: float) -> float:
    if value < 0 or value > 1:
        raise ValueError("confidence must be between 0 and 1")
    return float(value)


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip().casefold()


def _profile_key(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_source_refs(existing: list[SourceRef], incoming: list[SourceRef]) -> list[SourceRef]:
    merged: list[SourceRef] = []
    seen: set[tuple] = set()
    for ref in [*existing, *incoming]:
        key = tuple((field, getattr(ref, field)) for field in SourceRef.__dataclass_fields__)
        if key in seen:
            continue
        seen.add(key)
        merged.append(ref)
    return merged
