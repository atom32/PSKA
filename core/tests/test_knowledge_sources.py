from __future__ import annotations

from pathlib import Path

from pska_core.config import FilesConfig, PSKAConfig
from pska_core.files_connector import scan_files
from pska_core.knowledge_sources import KnowledgeSourceService
from pska_core.store import InMemoryKnowledgeStore


def test_seed_from_config_creates_folder_sources_once(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    config = PSKAConfig(files=FilesConfig(roots=(root,), ignore=("*.tmp",)))
    store = InMemoryKnowledgeStore()
    service = KnowledgeSourceService(store)

    first = service.seed_from_config(config)
    second = service.seed_from_config(config)

    assert len(first) == 1
    assert second == []
    sources = service.list_sources(owner_user_id="user_primary")
    assert len(sources) == 1
    assert sources[0].source_type == "folder"
    assert sources[0].config["path"] == str(root.resolve())
    assert sources[0].config["ignore"] == ["*.tmp"]


def test_seed_from_config_adds_config_roots_when_other_sources_exist(tmp_path: Path) -> None:
    old_root = tmp_path / "old_notes"
    new_root = tmp_path / "configured_notes"
    old_root.mkdir()
    new_root.mkdir()
    config = PSKAConfig(files=FilesConfig(roots=(new_root,), ignore=("*.tmp",), max_bytes=5_000_000))
    store = InMemoryKnowledgeStore()
    service = KnowledgeSourceService(store)
    service.add_folder_source(old_root, max_bytes=1_000_000)

    seeded = service.seed_from_config(config)

    assert len(seeded) == 1
    sources = service.list_sources(owner_user_id="user_primary")
    assert len(sources) == 2
    configured = next(source for source in sources if source.config["path"] == str(new_root.resolve()))
    assert configured.config["ignore"] == ["*.tmp"]
    assert configured.config["max_bytes"] == 5_000_000


def test_seed_from_config_updates_existing_config_root(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    store = InMemoryKnowledgeStore()
    service = KnowledgeSourceService(store)
    service.add_folder_source(root, ignore=["*.bak"], max_bytes=1_000_000)
    config = PSKAConfig(files=FilesConfig(roots=(root,), ignore=("*.tmp",), max_bytes=5_000_000))

    seeded = service.seed_from_config(config)

    assert len(seeded) == 1
    source = service.list_sources(owner_user_id="user_primary")[0]
    assert source.config["ignore"] == ["*.tmp"]
    assert source.config["max_bytes"] == 5_000_000


def test_record_sync_report_updates_source_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "note.md").write_text("PSKA Knowledge Source lifecycle.", encoding="utf-8")
    store = InMemoryKnowledgeStore()
    service = KnowledgeSourceService(store)
    source = service.add_folder_source(root)

    report = scan_files(store, root=root)
    run = service.record_sync_report(source, report)

    updated = store.get_knowledge_source(source.knowledge_source_id)
    assert run.scanned == 1
    assert run.new_files == 1
    assert run.status == "succeeded"
    assert updated.status == "indexed"
    assert updated.last_sync_at is not None


def test_record_sync_report_writes_processing_spans(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "note.md").write_text("# Title\n\nPSKA chunk preview and digest lineage.", encoding="utf-8")
    store = InMemoryKnowledgeStore()
    service = KnowledgeSourceService(store)
    source = service.add_folder_source(root)

    report = scan_files(store, root=root, processing_config={"chunking": {"strategy": "heading", "chunk_size": 80}})
    run = service.record_sync_report(source, report)
    spans = store.list_processing_spans(sync_run_id=run.sync_run_id)

    assert run.report["effective_processing_config"]["chunking"]["strategy"] == "heading"
    assert {span.stage for span in spans} == {"discover", "extract", "chunk", "embed", "index", "digest"}
    assert next(span for span in spans if span.stage == "chunk").metadata["chunking"]["chunk_size"] == 80
    assert next(span for span in spans if span.stage == "digest").status == "pending"
