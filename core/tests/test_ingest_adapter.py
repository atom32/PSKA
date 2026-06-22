from __future__ import annotations

from pska_core.adapters.twitter_archive import archive_metadata_to_payload
from pska_core.ingest import IngestService
from pska_core.store import InMemoryKnowledgeStore


def test_twitter_archive_metadata_converts_to_core_payload() -> None:
    metadata = {
        "schema_version": "pska.archive.v2",
        "source": "twitter",
        "record_type": "tweet",
        "source_id": "123",
        "url": "https://x.com/u/status/123",
        "canonical_url": "https://x.com/u/status/123",
        "author": {"handle": "@u", "profile_url": "https://x.com/u"},
        "content": {"text": "hello archive", "raw_text": "hello archive", "language": None},
        "comments": [],
        "metrics": {},
        "artifacts": {
            "metadata": "metadata.json",
            "markdown": "content.md",
            "comments": "comments.json",
            "raw_html": "raw.html",
            "screenshot": "screenshot.png",
        },
        "pska": {
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "visible_team_ids": [],
        },
    }

    payload = archive_metadata_to_payload(
        metadata,
        owner_user_id="user_primary",
        space_id="private_primary",
    )

    assert payload.schema_version == "pska.channel_ingest.v1"
    assert payload.source_channel == "twitter"
    assert payload.source_id == "123"
    assert payload.visibility == "private"
    assert payload.raw_paths["metadata"] == "metadata.json"


def test_legacy_twitter_zip_metadata_converts_without_being_canonical() -> None:
    metadata = {
        "id": "legacy-123",
        "url": "https://x.com/u/status/legacy-123",
        "author": "User",
        "handle": "@u",
        "content": "legacy text",
        "created_at": "2026-06-10T00:00:00.000Z",
        "images": ["https://example.invalid/image.jpg"],
        "videos": [],
    }

    payload = archive_metadata_to_payload(
        metadata,
        owner_user_id="user_primary",
        space_id="private_primary",
    )

    assert payload.source_id == "legacy-123"
    assert payload.content["text"] == "legacy text"
    assert payload.extra["archive_schema_version"] == "legacy.twitter_zip"


def test_duplicate_ingest_is_idempotent() -> None:
    store = InMemoryKnowledgeStore()
    ingest = IngestService(store)
    payload = {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "note",
        "source_id": "same",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "content": {"text": "same content"},
    }

    first = ingest.ingest_channel_payload(payload)
    second = ingest.ingest_channel_payload(payload)

    assert first.source_item_id == second.source_item_id
    assert len(store.source_items) == 1
    assert len(store.documents) == 1
    assert len(store.chunks) == 1


def test_ingest_chunks_with_configured_overlap() -> None:
    store = InMemoryKnowledgeStore()
    ingest = IngestService(store, chunk_size=6, chunk_overlap=2)
    payload = {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "note",
        "source_id": "overlap",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "content": {"text": "abcdefghijkl"},
    }

    item = ingest.ingest_channel_payload(payload)
    chunks = store.list_chunks_for_sources({item.source_item_id})

    assert [chunk.text for chunk in chunks] == ["abcdef", "efghij", "ijkl"]
    assert all(chunk.metadata["chunk_size"] == 6 for chunk in chunks)
    assert all(chunk.metadata["chunk_overlap"] == 2 for chunk in chunks)


def test_ingest_replaces_nul_bytes_before_storing_text_and_metadata() -> None:
    store = InMemoryKnowledgeStore()

    item = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "files",
            "record_type": "file",
            "source_id": "nul-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "title": "nul\x00title",
            "content": {
                "text": "before\x00after",
                "nested": {"raw": "inner\x00value"},
                "items": ["list\x00value"],
            },
        }
    )
    chunks = store.list_chunks_for_sources({item.source_item_id})

    assert "\x00" not in item.title
    assert "\x00" not in item.content_text
    assert item.content_text == "before\uFFFDafter"
    assert item.metadata["content"]["text"] == "before\uFFFDafter"
    assert item.metadata["content"]["nested"]["raw"] == "inner\uFFFDvalue"
    assert item.metadata["content"]["items"] == ["list\uFFFDvalue"]
    assert all("\x00" not in chunk.text for chunk in chunks)
