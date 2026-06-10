from __future__ import annotations

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
