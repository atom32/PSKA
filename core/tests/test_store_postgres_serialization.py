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


def test_connector_state_from_row_restores_scope_and_config() -> None:
    store = PostgresKnowledgeStore("postgresql:///unused")

    state = store._connector_state_from_row(
        {
            "connector_state_id": "conn_user_primary_files",
            "connector_id": "files",
            "owner_user_id": "user_primary",
            "enabled": True,
            "scan_cursor": "cursor_1",
            "sync_status": "succeeded",
            "last_success_at": None,
            "last_error_at": None,
            "last_error": None,
            "permission_scope": {"roots": ["/Users/example/notes"]},
            "config": {"ignore": ["*.tmp"]},
            "created_at": None,
            "updated_at": None,
        }
    )

    assert state.connector_state_id == "conn_user_primary_files"
    assert state.scan_cursor == "cursor_1"
    assert state.permission_scope["roots"] == ["/Users/example/notes"]
    assert state.config["ignore"] == ["*.tmp"]
