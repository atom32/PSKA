from __future__ import annotations

from pska_core.connectors import (
    CONNECTOR_RECORD_SCHEMA_VERSION,
    CONNECTOR_STATE_SCHEMA_VERSION,
    connector_state_from_mapping,
    connector_record_to_payload,
)
from pska_core.ingest import IngestService
from pska_core.store import InMemoryKnowledgeStore


def test_connector_record_converts_to_channel_payload_and_ingests() -> None:
    record = {
        "schema_version": CONNECTOR_RECORD_SCHEMA_VERSION,
        "connector_id": "files",
        "external_id": "/Users/example/notes/pska.md",
        "source_uri": "file:///Users/example/notes/pska.md",
        "record_type": "file",
        "title": "PSKA note",
        "body": "Connector contract keeps source refs and permission metadata.",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "artifacts": {"path": "/Users/example/notes/pska.md"},
        "permission_metadata": {"root_id": "notes", "read_scope": "explicit_file"},
        "scan_cursor": "cursor_1",
        "content_hash": "sha256:abc",
        "metadata": {"mime_type": "text/markdown"},
    }

    payload = connector_record_to_payload(record)
    source = IngestService(InMemoryKnowledgeStore()).ingest_channel_payload(payload)

    assert payload.source_channel == "files"
    assert payload.source_id == "/Users/example/notes/pska.md"
    assert payload.url == "file:///Users/example/notes/pska.md"
    assert payload.raw_paths["path"] == "/Users/example/notes/pska.md"
    assert payload.extra["connector"]["scan_cursor"] == "cursor_1"
    assert payload.extra["permission_metadata"]["root_id"] == "notes"
    assert payload.extra["mime_type"] == "text/markdown"
    assert source.source_channel == "files"
    assert source.metadata["extra"]["connector"]["content_hash"] == "sha256:abc"


def test_connector_record_rejects_missing_body() -> None:
    try:
        connector_record_to_payload(
            {
                "schema_version": CONNECTOR_RECORD_SCHEMA_VERSION,
                "connector_id": "files",
                "external_id": "empty",
                "owner_user_id": "user_primary",
                "space_id": "private_primary",
            }
        )
    except ValueError as exc:
        assert "body" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_connector_state_is_durable_by_owner_and_connector() -> None:
    store = InMemoryKnowledgeStore()
    state = connector_state_from_mapping(
        {
            "schema_version": CONNECTOR_STATE_SCHEMA_VERSION,
            "connector_id": "files",
            "owner_user_id": "user_primary",
            "enabled": True,
            "scan_cursor": "cursor_1",
            "permission_scope": {"roots": ["/Users/example/notes"]},
            "config": {"ignore": ["*.tmp"]},
        }
    )

    saved = store.upsert_connector_state(state)
    listed = store.list_connector_states(owner_user_id="user_primary", connector_id="files")

    assert saved.connector_state_id == "conn_user_primary_files"
    assert listed == [saved]
    assert listed[0].scan_cursor == "cursor_1"
    assert listed[0].permission_scope["roots"] == ["/Users/example/notes"]
