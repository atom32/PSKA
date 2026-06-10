from __future__ import annotations

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.models import User
from pska_core.retrieval import RetrievalService
from pska_core.serde import dumps, to_jsonable
from pska_core.store import InMemoryKnowledgeStore
from pska_core.llm import LLMClient


DEMO_DOCUMENT = """# Team Planning Note

Project Atlas is the shared knowledge-base initiative.
The policy P-204 covers the education enrollment stage for dependent K.
Project Atlas depends on the Twitter Archive channel for social knowledge capture.
The Review Agent must confirm any team-visible sharing before private notes become team-visible.
"""


def build_demo(llm: LLMClient | None = None) -> dict:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))

    source_item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "planning_note",
            "source_id": "demo-planning-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Team Planning Note",
            "content": {"text": DEMO_DOCUMENT},
            "raw_paths": {"markdown": "demo://team-planning-note.md"},
        }
    )

    extraction_report = ExtractionService(store, llm=llm).extract_source_item(source_item)

    query = "What covers dependent K during education enrollment?"
    retrieval = RetrievalService(store, ACLService(store))
    agentic = AgenticSearchService(retrieval, llm=llm).search(query, store.get_user("user_primary"))
    return {
        "document": DEMO_DOCUMENT,
        "source_item_id": source_item.source_item_id,
        "extraction_report": to_jsonable(extraction_report),
        "entities": [to_jsonable(entity) for entity in store.list_entities()],
        "review_items": to_jsonable(store.list_review_items()),
        "question": query,
        "answer": agentic.answer,
        "agentic_trace": to_jsonable(agentic.trace),
        "citations": to_jsonable(agentic.retrieval.citations),
        "hypergraph_context": to_jsonable(agentic.retrieval.hypergraph_context),
    }


def main() -> int:
    print(dumps(build_demo()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
