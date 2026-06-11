from __future__ import annotations

from typing import Any
from uuid import uuid4

from pska_core.enums import ReviewType, Visibility
from pska_core.models import AuditEvent, ReviewItem, SourceRef, UserProfileCard
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

        if review_item.review_type == ReviewType.PROFILE_UPDATE:
            self._apply_profile_update(review_item)
        elif review_item.review_type == ReviewType.SHARE_PROPOSAL:
            self._apply_share_proposal(review_item)
        else:
            raise ValueError(f"Review type cannot be applied yet: {review_item.review_type.value}")

        updated = self.store.update_review_item_status(review_item_id, REVIEW_APPLIED)
        self._audit(updated, actor_user_id=actor_user_id, action="review.apply", decision=REVIEW_APPLIED, reason=reason)
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

    def _audit(
        self,
        review_item: ReviewItem,
        *,
        actor_user_id: str,
        action: str,
        decision: str,
        reason: str,
    ) -> None:
        self.store.add_audit_event(
            AuditEvent(
                audit_event_id=f"aud_{uuid4().hex}",
                actor_user_id=actor_user_id,
                action=action,
                target_type="review_item",
                target_id=review_item.review_item_id,
                decision=decision,
                metadata={"reason": reason, "review_type": review_item.review_type.value},
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
