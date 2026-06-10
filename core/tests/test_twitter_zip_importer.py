from __future__ import annotations

import json
import zipfile

from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.store import InMemoryKnowledgeStore


def test_v2_zip_imports_payload_with_artifact_paths(tmp_path) -> None:
    zip_path = tmp_path / "tweet.zip"
    metadata = {
        "schema_version": "pska.archive.v2",
        "source": "twitter",
        "record_type": "tweet",
        "source_id": "123",
        "url": "https://x.com/u/status/123",
        "canonical_url": "https://x.com/u/status/123",
        "author": {"handle": "@u"},
        "content": {"text": "zip import citation text", "raw_text": "zip import citation text"},
        "created_at": None,
        "captured_at": None,
        "media": [],
        "comments": [],
        "quoted_items": [],
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
        "extraction": {"status": "ok", "warnings": [], "source": "visible_dom"},
        "extra": {},
    }
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("123/metadata.json", json.dumps(metadata))
        archive.writestr("123/content.md", "zip import citation text")
        archive.writestr("123/comments.json", "[]")
        archive.writestr("123/raw.html", "<html></html>")
        archive.writestr("123/screenshot.png", b"png")

    store = InMemoryKnowledgeStore()
    item = TwitterZipImporter(store, archive_root=tmp_path / "archive").import_zip(zip_path)

    assert item.source_id == "123"
    assert item.content_text == "zip import citation text"
    assert item.metadata["raw_paths"]["metadata"].endswith("123/metadata.json")
    assert len(store.documents) == 1
    assert len(store.chunks) == 1


def test_legacy_zip_imports_as_legacy_schema(tmp_path) -> None:
    zip_path = tmp_path / "legacy.zip"
    metadata = {
        "id": "legacy-1",
        "url": "https://x.com/u/status/legacy-1",
        "author": "User",
        "handle": "@u",
        "content": "legacy zip text",
        "images": [],
        "videos": [],
    }
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("legacy-1/metadata.json", json.dumps(metadata))
        archive.writestr("legacy-1/content.md", "legacy zip text")

    store = InMemoryKnowledgeStore()
    item = TwitterZipImporter(store, archive_root=tmp_path / "archive").import_zip(zip_path)

    assert item.source_id == "legacy-1"
    assert item.metadata["extra"]["archive_schema_version"] == "legacy.twitter_zip"


def test_duplicate_zip_import_is_idempotent(tmp_path) -> None:
    zip_path = tmp_path / "tweet.zip"
    metadata = {
        "schema_version": "pska.archive.v2",
        "source": "twitter",
        "record_type": "tweet",
        "source_id": "dup",
        "url": "https://x.com/u/status/dup",
        "content": {"text": "same zip text"},
        "pska": {"owner_user_id": "user_primary", "space_id": "private_primary", "visibility": "private"},
    }
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("dup/metadata.json", json.dumps(metadata))

    store = InMemoryKnowledgeStore()
    importer = TwitterZipImporter(store, archive_root=tmp_path / "archive")
    first = importer.import_zip(zip_path)
    second = importer.import_zip(zip_path)

    assert first.source_item_id == second.source_item_id
    assert len(store.source_items) == 1
    assert len(store.documents) == 1
    assert len(store.chunks) == 1
