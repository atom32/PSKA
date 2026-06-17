from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pska_core.enums import Directionality, ReviewType, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.models import AuditEvent, Entity, ReviewItem, SourceRef, UserProfileCard
from pska_core.store import KnowledgeStore


REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_APPLIED = "applied"
REVIEW_EXPIRED = "expired"

TERMINAL_REVIEW_STATUSES = {REVIEW_REJECTED, REVIEW_APPLIED, REVIEW_EXPIRED}


class ReviewService:
    """Approval workflow for sensitive knowledge changes."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def approve(self, review_item_id: str, *, actor_user_id: str, reason: str = "") -> ReviewItem:
        review_item = self.store.get_review_item(review_item_id)
        self._require_status(review_item, {REVIEW_PENDING})
        updated = self.store.update_review_item_status(review_item_id, REVIEW_APPROVED)
        self._audit(updated, actor_user_id=actor_user_id, action="review.approve", decision=REVIEW_APPROVED, reason=reason)
        return updated

    def reject(self, review_item_id: str, *, actor_user_id: str, reason: str = "") -> ReviewItem:
        review_item = self.store.get_review_item(review_item_id)
        self._require_status(review_item, {REVIEW_PENDING})
        updated = self.store.update_review_item_status(review_item_id, REVIEW_REJECTED)
        self._audit(updated, actor_user_id=actor_user_id, action="review.reject", decision=REVIEW_REJECTED, reason=reason)
        return updated

    def apply(self, review_item_id: str, *, actor_user_id: str, reason: str = "") -> ReviewItem:
        review_item = self.store.get_review_item(review_item_id)
        self._require_status(review_item, {REVIEW_APPROVED})

        apply_metadata: dict[str, Any] = {}
        if review_item.review_type == ReviewType.PROFILE_UPDATE:
            self._apply_profile_update(review_item)
        elif review_item.review_type == ReviewType.SHARE_PROPOSAL:
            self._apply_share_proposal(review_item)
        elif review_item.review_type == ReviewType.RELATIONSHIP_CANDIDATE:
            apply_metadata = self._apply_relationship_candidate(review_item)
        else:
            raise ValueError(f"Review type cannot be applied yet: {review_item.review_type.value}")

        updated = self.store.update_review_item_status(review_item_id, REVIEW_APPLIED)
        self._audit(updated, actor_user_id=actor_user_id, action="review.apply", decision=REVIEW_APPLIED, reason=reason, metadata=apply_metadata)
        return updated

    def approve_and_apply(self, review_item_id: str, *, actor_user_id: str, reason: str = "") -> ReviewItem:
        approved = self.approve(review_item_id, actor_user_id=actor_user_id, reason=reason)
        return self.apply(approved.review_item_id, actor_user_id=actor_user_id, reason=reason)

    def expire(self, review_item_id: str, *, actor_user_id: str, reason: str = "") -> ReviewItem:
        review_item = self.store.get_review_item(review_item_id)
        self._require_status(review_item, {REVIEW_PENDING, REVIEW_APPROVED})
        updated = self.store.update_review_item_status(review_item_id, REVIEW_EXPIRED)
        self._audit(updated, actor_user_id=actor_user_id, action="review.expire", decision=REVIEW_EXPIRED, reason=reason)
        return updated

    def _apply_profile_update(self, review_item: ReviewItem) -> None:
        proposal = review_item.proposal
        profile_delta = proposal.get("profile_delta") or proposal.get("profile")
        if not isinstance(profile_delta, dict) or not profile_delta:
            raise ValueError("Profile update review requires a non-empty profile_delta")

        source_refs = [_source_ref_from_dict(item) for item in _list_of_dicts(proposal.get("source_refs"))]
        card = UserProfileCard(
            profile_card_id=f"upc_{uuid4().hex}",
            owner_user_id=review_item.owner_user_id,
            profile=profile_delta,
            source_refs=source_refs,
            confidence=float(proposal.get("confidence", 0.8)),
        )
        self.store.add_profile_card(card)

    def _apply_share_proposal(self, review_item: ReviewItem) -> None:
        proposal = review_item.proposal
        target_type = proposal.get("target_type")
        target_id = proposal.get("target_id")
        if not isinstance(target_type, str) or not isinstance(target_id, str):
            raise ValueError("Share proposal requires target_type and target_id before it can be applied")

        visibility = proposal.get("visibility", Visibility.TEAM.value)
        if isinstance(visibility, Visibility):
            visibility_value = visibility.value
        elif isinstance(visibility, str):
            visibility_value = Visibility(visibility).value
        else:
            raise ValueError("Share proposal visibility must be a string")

        visible_team_ids = proposal.get("visible_team_ids", [])
        if isinstance(visible_team_ids, str):
            visible_team_ids = [item.strip() for item in visible_team_ids.split(",") if item.strip()]
        if not isinstance(visible_team_ids, list) or not all(isinstance(item, str) for item in visible_team_ids):
            raise ValueError("Share proposal visible_team_ids must be a list or comma-separated string")
        if visibility_value == Visibility.TEAM.value and not visible_team_ids:
            raise ValueError("Team-visible share proposal requires visible_team_ids")

        self.store.update_visibility(
            target_type=target_type,
            target_id=target_id,
            visibility=visibility_value,
            visible_team_ids=visible_team_ids,
        )

    def _apply_relationship_candidate(self, review_item: ReviewItem) -> dict[str, Any]:
        proposal = review_item.proposal
        relation_type = str(proposal.get("relation_type") or "").strip()
        if not relation_type:
            raise ValueError("Relationship candidate review requires relation_type")
        source_refs = [_source_ref_from_dict(item) for item in _list_of_dicts(proposal.get("source_refs"))]
        if not source_refs:
            raise ValueError("Relationship candidate review requires source_refs before it can be applied")
        members = self._relationship_members(review_item)
        if len(members) < 2:
            raise ValueError("Relationship candidate review requires at least two members")
        confidence = float(proposal.get("confidence", 0.0))
        if confidence <= 0 or confidence > 1:
            raise ValueError("Relationship candidate review confidence must be between 0 and 1")
        anchor = self._source_item_for_refs(source_refs)
        evidence_text = str(proposal.get("evidence_text") or proposal.get("evidence") or review_item.title)
        edge = HypergraphService(self.store).create_hyperedge(
            relation_type=relation_type,
            owner_user_id=review_item.owner_user_id,
            space_id=str(proposal.get("space_id") or anchor.space_id),
            visibility=_visibility(proposal.get("visibility"), anchor.visibility),
            visible_team_ids=list(proposal.get("visible_team_ids") or anchor.visible_team_ids),
            directionality=Directionality(str(proposal.get("directionality") or Directionality.AMBIGUOUS.value)),
            members=members,
            evidence_text=evidence_text,
            source_refs=source_refs,
            confidence=confidence,
        )
        return {
            "created_hyperedge_id": edge.hyperedge_id,
            "relation_type": edge.relation_type,
            "source_refs": [asdict(item) for item in source_refs],
            "confidence": edge.confidence,
        }

    def _relationship_members(self, review_item: ReviewItem) -> list[tuple[str, str]]:
        proposal = review_item.proposal
        members: list[tuple[str, str]] = []
        for member in _list_of_dicts(proposal.get("members")):
            role = str(member.get("role") or "related")
            entity_id = member.get("entity_id")
            if not entity_id:
                entity_type = str(member.get("entity_type") or "").strip()
                label = str(member.get("label") or "").strip()
                if not entity_type or not label:
                    raise ValueError("Relationship candidate members require entity_id or entity_type and label")
                entity_id = f"ent_{uuid5(NAMESPACE_URL, '|'.join([review_item.owner_user_id, entity_type, label])).hex}"
                self.store.add_entity(
                    Entity(
                        entity_id=str(entity_id),
                        entity_type=entity_type,
                        label=label,
                        owner_user_id=review_item.owner_user_id,
                        space_id=str(proposal.get("space_id") or "private_primary"),
                        visibility=_visibility(proposal.get("visibility"), Visibility.PRIVATE),
                    )
                )
            members.append((str(entity_id), role))
        return members

    def _source_item_for_refs(self, source_refs: list[SourceRef]):
        source_item_ids = {ref.source_item_id for ref in source_refs if ref.source_item_id}
        for item in self.store.list_source_items():
            if item.source_item_id in source_item_ids:
                return item
        raise ValueError("Relationship candidate source_refs must reference an existing source_item")

    def _audit(
        self,
        review_item: ReviewItem,
        *,
        actor_user_id: str,
        action: str,
        decision: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.store.add_audit_event(
            AuditEvent(
                audit_event_id=f"aud_{uuid4().hex}",
                actor_user_id=actor_user_id,
                action=action,
                target_type="review_item",
                target_id=review_item.review_item_id,
                decision=decision,
                metadata={"reason": reason, "review_type": review_item.review_type.value, **(metadata or {})},
            )
        )

    def _require_status(self, review_item: ReviewItem, allowed: set[str]) -> None:
        if review_item.status not in allowed:
            allowed_text = ", ".join(sorted(allowed))
            raise ValueError(f"Review item {review_item.review_item_id} is {review_item.status}; expected {allowed_text}")


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _source_ref_from_dict(value: dict[str, Any]) -> SourceRef:
    allowed_keys = set(SourceRef.__dataclass_fields__)
    return SourceRef(**{key: item for key, item in value.items() if key in allowed_keys})


def _visibility(value: Any, default: Visibility) -> Visibility:
    if isinstance(value, Visibility):
        return value
    if value:
        return Visibility(str(value))
    return default
