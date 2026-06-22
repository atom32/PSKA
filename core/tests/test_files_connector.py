from __future__ import annotations

from pathlib import Path
import sys

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


def test_files_scan_recognizes_optional_document_extractors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    monkeypatch.setitem(sys.modules, "docx", None)
    root = tmp_path / "notes"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "memo.docx").write_bytes(b"not a real docx")

    report = scan_files(InMemoryKnowledgeStore(), root=root)

    assert report.ingested == 0
    assert {item["reason"] for item in report.skipped} == {"missing_dependency"}
    assert all("required" in item["detail"] for item in report.skipped)


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


def test_files_scan_reconciles_unchanged_changed_moved_and_missing_files(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "note.txt"
    note.write_text("stable content", encoding="utf-8")
    store = InMemoryKnowledgeStore()

    first = scan_files(store, root=root)
    first_source_id = first.source_item_ids[0]
    first_manifest = dict(first.connector_state.config["files_manifest"])

    unchanged = scan_files(store, root=root)
    note.write_text("changed content", encoding="utf-8")
    changed = scan_files(store, root=root)
    changed_source_id = changed.source_item_ids[0]
    moved_path = root / "renamed.txt"
    note.rename(moved_path)
    moved = scan_files(store, root=root)
    moved_path.unlink()
    missing = scan_files(store, root=root)

    assert first.new_files == 1
    assert first_manifest["note.txt"]["source_item_id"] == first_source_id
    assert unchanged.unchanged_files == 1
    assert unchanged.ingested == 0
    assert changed.changed_files == 1
    assert changed_source_id != first_source_id
    assert moved.moved_files == 1
    assert moved.missing_files == 0
    assert moved.source_item_ids == [changed_source_id]
    assert moved.changes[0]["previous_path"] == str(note.resolve())
    assert missing.missing_files == 1
    assert missing.connector_state.config["files_missing"][0]["source_item_id"] == changed_source_id


def test_files_scan_keeps_manifests_isolated_by_root(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "a.txt").write_text("first root", encoding="utf-8")
    (second_root / "b.txt").write_text("second root", encoding="utf-8")
    store = InMemoryKnowledgeStore()

    first = scan_files(store, root=first_root)
    second = scan_files(store, root=second_root)
    first_again = scan_files(store, root=first_root)

    assert first.missing_files == 0
    assert second.missing_files == 0
    assert first_again.missing_files == 0
    assert first_again.unchanged_files == 1
    manifests_by_root = first_again.connector_state.config["files_manifests_by_root"]
    assert set(manifests_by_root) == {str(first_root.resolve()), str(second_root.resolve())}
