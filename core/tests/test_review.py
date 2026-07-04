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

    assert review.proposal["plain_text_summary"] == "Profile update requires human review."
    rejected = ReviewService(store).reject(review.review_item_id, actor_user_id="user_primary", reason="not now")

    assert rejected.status == "rejected"
    assert store.profile_cards == {}
    events = store.list_audit_events("review_item", review.review_item_id)
    assert [event.decision for event in events] == ["rejected"]
    assert events[0].metadata["reason"] == "not now"


def test_snoozed_review_can_be_restored_to_pending_with_audit() -> None:
    store = _store()
    review = MemoryService(store).propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"timezone": "Asia/Shanghai"},
        source_refs=[SourceRef(message_id="msg_snooze")],
        sensitivity="high",
    )

    service = ReviewService(store)
    snoozed = service.snooze(review.review_item_id, actor_user_id="user_primary", reason="later")
    assert snoozed.status == "snoozed"
    with pytest.raises(ValueError, match="expected pending"):
        service.approve(review.review_item_id, actor_user_id="user_primary")

    restored = service.restore(review.review_item_id, actor_user_id="user_primary", reason="ready")
    assert restored.status == "pending"
    approved = service.approve(review.review_item_id, actor_user_id="user_primary")
    assert approved.status == "approved"
    assert [event.decision for event in store.list_audit_events("review_item", review.review_item_id)] == [
        "snoozed",
        "pending",
        "approved",
    ]


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


def test_repeated_profile_update_merges_source_refs_and_confidence() -> None:
    store = _store()
    service = MemoryService(store)
    first = service.propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"communication": "concise"},
        source_refs=[SourceRef(message_id="msg_1")],
        sensitivity="sensitive",
        confidence=0.6,
    )
    second = service.propose_profile_update(
        owner_user_id="user_primary",
        profile_delta={"communication": "concise"},
        source_refs=[SourceRef(message_id="msg_2")],
        sensitivity="sensitive",
        confidence=0.9,
    )

    ReviewService(store).approve_and_apply(first.review_item_id, actor_user_id="user_primary")
    ReviewService(store).approve_and_apply(second.review_item_id, actor_user_id="user_primary")

    assert len(store.profile_cards) == 1
    card = next(iter(store.profile_cards.values()))
    assert card.confidence == 0.9
    assert card.last_verified_at is not None
    assert card.source_refs == [SourceRef(message_id="msg_1"), SourceRef(message_id="msg_2")]
    second_events = store.list_audit_events("review_item", second.review_item_id)
    assert second_events[-1].metadata["action"] == "updated"
    assert second_events[-1].metadata["source_refs_merged"] == 1


def test_memory_candidate_review_promotes_agent_memory_with_audit() -> None:
    store = _store()
    review = ReviewItem(
        review_item_id="rev_memory_candidate",
        owner_user_id="user_primary",
        review_type=ReviewType.MEMORY_CANDIDATE,
        title="Review memory candidate",
        proposal={
            "memory_candidate": "PSKA prefers evidence-first answers.",
            "layer": "semantic",
            "confidence": 0.72,
            "source_refs": [{"message_id": "msg_memory"}],
        },
    )
    store.add_review_item(review)

    applied = ReviewService(store).approve_and_apply(review.review_item_id, actor_user_id="user_primary")

    assert applied.status == "applied"
    assert len(store.agent_memories) == 1
    memory = next(iter(store.agent_memories.values()))
    assert memory.text == "PSKA prefers evidence-first answers."
    assert memory.confidence == 0.72
    assert memory.last_verified_at is not None
    assert memory.source_refs == [SourceRef(message_id="msg_memory")]
    events = store.list_audit_events("review_item", review.review_item_id)
    assert events[-1].metadata["promotion_type"] == "agent_memory"
    assert events[-1].metadata["action"] == "created"
    assert events[-1].metadata["agent_memory_id"] == memory.agent_memory_id


def test_repeated_low_confidence_memory_candidate_updates_existing_memory() -> None:
    store = _store()
    reviews = [
        ReviewItem(
            review_item_id=f"rev_low_{index}",
            owner_user_id="user_primary",
            review_type=ReviewType.LOW_CONFIDENCE,
            title="Review low-confidence memory",
            proposal={
                "memory_candidate": "PSKA likes grounded summaries.",
                "layer": "semantic",
                "confidence": confidence,
                "source_refs": [{"message_id": message_id}],
            },
        )
        for index, (confidence, message_id) in enumerate([(0.4, "msg_a"), (0.8, "msg_b")], start=1)
    ]
    for review in reviews:
        store.add_review_item(review)
        ReviewService(store).approve_and_apply(review.review_item_id, actor_user_id="user_primary")

    assert len(store.agent_memories) == 1
    memory = next(iter(store.agent_memories.values()))
    assert memory.confidence == 0.8
    assert memory.source_refs == [SourceRef(message_id="msg_a"), SourceRef(message_id="msg_b")]
    second_events = store.list_audit_events("review_item", "rev_low_2")
    assert second_events[-1].metadata["action"] == "updated"
    assert second_events[-1].metadata["source_refs_merged"] == 1


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


def test_relationship_candidate_without_source_refs_cannot_apply() -> None:
    store = _store()
    review = ReviewItem(
        review_item_id="rev_rel_missing_refs",
        owner_user_id="user_primary",
        review_type=ReviewType.RELATIONSHIP_CANDIDATE,
        title="Review relationship without refs",
        proposal={
            "relation_type": "depends_on",
            "confidence": 0.8,
            "members": [
                {"entity_type": "project", "label": "PSKA", "role": "system"},
                {"entity_type": "service", "label": "FastReAct", "role": "dependency"},
            ],
        },
    )
    store.add_review_item(review)

    ReviewService(store).approve(review.review_item_id, actor_user_id="user_primary")
    with pytest.raises(ValueError, match="source_refs"):
        ReviewService(store).apply(review.review_item_id, actor_user_id="user_primary")

    assert store.get_review_item(review.review_item_id).status == "approved"
    assert store.hyperedges == {}


def test_relationship_candidate_applies_hyperedge_with_evidence_and_audit() -> None:
    store = _store()
    source = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "note-relationship",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Relationship note",
            "content": {"text": "PSKA depends on FastReAct for narrative briefing."},
        }
    )
    review = ReviewItem(
        review_item_id="rev_rel_apply",
        owner_user_id="user_primary",
        review_type=ReviewType.RELATIONSHIP_CANDIDATE,
        title="Review PSKA FastReAct relationship",
        proposal={
            "relation_type": "depends_on",
            "evidence_text": "PSKA depends on FastReAct for narrative briefing.",
            "confidence": 0.82,
            "source_refs": [{"source_item_id": source.source_item_id}],
            "members": [
                {"entity_type": "project", "label": "PSKA", "role": "system"},
                {"entity_type": "service", "label": "FastReAct", "role": "dependency"},
            ],
        },
    )
    store.add_review_item(review)

    applied = ReviewService(store).approve_and_apply(review.review_item_id, actor_user_id="user_primary")

    assert applied.status == "applied"
    edge = next(iter(store.hyperedges.values()))
    assert edge.relation_type == "depends_on"
    assert edge.evidence_text == "PSKA depends on FastReAct for narrative briefing."
    assert edge.source_refs == [SourceRef(source_item_id=source.source_item_id)]
    assert edge.confidence == 0.82
    events = store.list_audit_events("review_item", review.review_item_id)
    assert [event.decision for event in events] == ["approved", "applied"]
    assert events[-1].metadata["created_hyperedge_id"] == edge.hyperedge_id
    assert events[-1].metadata["source_refs"][0]["source_item_id"] == source.source_item_id


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    return store
