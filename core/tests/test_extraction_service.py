from __future__ import annotations

from pska_core.adapters.conversation import conversation_to_payload
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.models import User
from pska_core.store import InMemoryKnowledgeStore
from tests.fakes import FakeLLM, extraction_response


def test_extraction_creates_entities_hyperedges_and_review_items() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "extract-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {
                "text": (
                    "Project Atlas depends on the Twitter Archive channel. "
                    "The policy P-204 covers the education enrollment stage for dependent K. "
                    "The Review Agent must confirm any team-visible sharing before release."
                )
            },
        }
    )

    llm = FakeLLM([extraction_response()])
    report = ExtractionService(store, llm=llm).extract_source_item(item)

    labels = {entity.label for entity in store.list_entities()}
    relations = {edge.relation_type for edge in store.hyperedges.values()}
    assert {"Project Atlas", "Twitter Archive", "P-204", "dependent K", "education enrollment"} <= labels
    assert {"covers", "depends_on", "requires_review"} <= relations
    assert report.review_items_created
    assert "knowledge extraction agent" in llm.prompts[0]["system"]


def test_conversation_review_item_preserves_message_provenance() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    payload = conversation_to_payload(
        {
            "id": "conv_extract",
            "messages": [
                {
                    "id": "msg_pref",
                    "role": "user",
                    "participant_name": "Dawei",
                    "content": "Please remember that I prefer short answers.",
                    "created_at": "2026-06-11T11:00:00Z",
                }
            ],
        },
        owner_user_id="user_primary",
        space_id="private_primary",
    )
    item = IngestService(store).ingest_channel_payload(payload)
    llm = FakeLLM(
        [
            {
                "entities": [],
                "hyperedges": [],
                "review_items": [
                    {
                        "review_type": "profile_update",
                        "title": "Remember answer length preference",
                        "proposal": {
                            "profile_delta": {"answer_style": "short"},
                            "message_ids": ["msg_pref"],
                        },
                    }
                ],
            }
        ]
    )

    report = ExtractionService(store, llm=llm).extract_source_item(item)

    assert report.review_items_created
    review = store.review_items[report.review_items_created[0]]
    assert review.proposal["profile_delta"] == {"answer_style": "short"}
    assert any(ref["message_id"] == "msg_pref" for ref in review.proposal["source_refs"])
    assert "proposal.message_ids" in llm.prompts[0]["prompt"]
