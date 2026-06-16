from __future__ import annotations

from pathlib import Path

from pska_core.connectors import connector_state_from_mapping
from pska_core.files_connector import scan_files
from pska_core.store import InMemoryKnowledgeStore


def test_files_scan_ingests_text_files_and_updates_connector_state(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "project.md"
    note.write_text("# Project\n\nPSKA files connector keeps source refs.", encoding="utf-8")
    (root / "image.png").write_bytes(b"png")

    store = InMemoryKnowledgeStore()
    report = scan_files(store, root=root, owner_user_id="user_primary")

    assert report.scanned == 2
    assert report.ingested == 1
    assert report.source_item_ids
    assert report.connector_state.connector_state_id == "conn_user_primary_files"
    assert report.connector_state.scan_cursor
    source = store.source_items[report.source_item_ids[0]]
    assert source.source_channel == "files"
    assert source.url == note.resolve().as_uri()
    assert source.metadata["extra"]["permission_metadata"]["root"] == str(root.resolve())
    assert report.skipped == [{"path": str((root / "image.png").resolve()), "reason": "unsupported_suffix"}]


def test_files_scan_honors_ignore_and_size_limit(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "keep.txt").write_text("keep me", encoding="utf-8")
    (root / "ignore.txt").write_text("ignore me", encoding="utf-8")
    (root / "large.txt").write_text("too large", encoding="utf-8")

    report = scan_files(
        InMemoryKnowledgeStore(),
        root=root,
        ignore=["ignore.txt"],
        max_bytes=4,
    )

    assert report.scanned == 2
    assert report.ingested == 0
    assert {item["reason"] for item in report.skipped} == {"file_too_large"}


def test_files_scan_respects_disabled_connector_state(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "note.txt").write_text("keep me", encoding="utf-8")
    store = InMemoryKnowledgeStore()
    state = scan_files(store, root=root).connector_state
    state.enabled = False
    store.upsert_connector_state(state)

    report = scan_files(store, root=root)

    assert report.ingested == 0
    assert report.skipped == [{"root": str(root.resolve()), "reason": "connector_disabled"}]


def test_files_scan_updates_legacy_cursor_and_appends_authorized_root(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (second_root / "note.txt").write_text("new root", encoding="utf-8")
    store = InMemoryKnowledgeStore()
    store.upsert_connector_state(
        connector_state_from_mapping(
            {
                "connector_id": "files",
                "owner_user_id": "user_primary",
                "scan_cursor": "legacy_cursor",
                "permission_scope": {"roots": [str(first_root.resolve())]},
            }
        )
    )

    report = scan_files(store, root=second_root)

    assert report.ingested == 1
    assert report.connector_state.scan_cursor != "legacy_cursor"
    assert report.connector_state.permission_scope["roots"] == [str(first_root.resolve()), str(second_root.resolve())]
