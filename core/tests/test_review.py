from __future__ import annotations

import pytest

from pska_core.enums import ReviewType, UserRole, Visibility
from pska_core.ingest import IngestService
from pska_core.memory import MemoryService
from pska_core.models import ReviewItem, SourceRef, User
from pska_core.review import ReviewService
from pska_core.store import InMemoryKnowledgeStore


def test_reject_review_writes_audit_and_does_not_apply_profile() -> None:
    store = _store()
    review = MemoryService(store).propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"diet": "vegetarian"},
        source_refs=[SourceRef(message_id="msg_1")],
        sensitivity="high",
    )

    rejected = ReviewService(store).reject(review.review_item_id, actor_user_id="user_primary", reason="not now")

    assert rejected.status == "rejected"
    assert store.profile_cards == {}
    events = store.list_audit_events("review_item", review.review_item_id)
    assert [event.decision for event in events] == ["rejected"]
    assert events[0].metadata["reason"] == "not now"


def test_profile_update_applies_only_after_approval() -> None:
    store = _store()
    review = MemoryService(store).propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"communication": "concise"},
        source_refs=[SourceRef(message_id="msg_2")],
        sensitivity="sensitive",
    )

    assert store.profile_cards == {}
    applied = ReviewService(store).approve_and_apply(review.review_item_id, actor_user_id="user_primary")

    assert applied.status == "applied"
    assert len(store.profile_cards) == 1
    card = next(iter(store.profile_cards.values()))
    assert card.profile == {"communication": "concise"}
    assert card.source_refs[0].message_id == "msg_2"
    assert [event.decision for event in store.list_audit_events("review_item", review.review_item_id)] == [
        "approved",
        "applied",
    ]


def test_share_proposal_updates_visibility_only_when_applied() -> None:
    store = _store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note-share",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Private note",
            "content": {"text": "share this after approval"},
        }
    )
    review = ReviewItem(
        review_item_id="rev_share_1",
        owner_user_id="user_primary",
        review_type=ReviewType.SHARE_PROPOSAL,
        title="Share note with team",
        proposal={
            "target_type": "source_item",
            "target_id": source.source_item_id,
            "visibility": "team",
            "visible_team_ids": ["team_default"],
        },
    )
    store.add_review_item(review)

    approved = ReviewService(store).approve(review.review_item_id, actor_user_id="user_primary")
    assert approved.status == "approved"
    assert store.source_items[source.source_item_id].visibility == Visibility.PRIVATE

    applied = ReviewService(store).apply(review.review_item_id, actor_user_id="user_primary")
    assert applied.status == "applied"
    assert store.source_items[source.source_item_id].visibility == Visibility.TEAM
    assert store.source_items[source.source_item_id].visible_team_ids == ["team_default"]


def test_llm_share_proposal_without_target_cannot_apply() -> None:
    store = _store()
    review = ReviewItem(
        review_item_id="rev_share_llm",
        owner_user_id="user_primary",
        review_type=ReviewType.SHARE_PROPOSAL,
        title="Review team-visible sharing",
        proposal={"reason": "The document proposes team-visible sharing."},
    )
    store.add_review_item(review)

    ReviewService(store).approve(review.review_item_id, actor_user_id="user_primary")
    with pytest.raises(ValueError, match="target_type and target_id"):
        ReviewService(store).apply(review.review_item_id, actor_user_id="user_primary")

    assert store.get_review_item(review.review_item_id).status == "approved"


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    return store
