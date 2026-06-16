from __future__ import annotations

from pska_core.store_postgres import PostgresKnowledgeStore


def test_hyperedge_from_row_restores_source_refs() -> None:
    store = PostgresKnowledgeStore("postgresql:///unused")

    edge = store._hyperedge_from_row(
        {
            "hyperedge_id": "hed_1",
            "relation_type": "built_with",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "directionality": "directed",
            "visible_team_ids": [],
            "evidence_text": "Obscura is built with Rust.",
            "source_refs": [
                {
                    "source_item_id": "src_1",
                    "document_id": None,
                    "chunk_id": "chk_1",
                    "message_id": None,
                    "path": "/archive/src_1.md",
                    "url": "https://example.test/src_1",
                }
            ],
            "confidence": 0.9,
        }
    )

    assert len(edge.source_refs) == 1
    assert edge.source_refs[0].source_item_id == "src_1"
    assert edge.source_refs[0].chunk_id == "chk_1"
    assert edge.source_refs[0].url == "https://example.test/src_1"
