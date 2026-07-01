from __future__ import annotations

import json
from pathlib import Path
import sys
import types
import zipfile

from pska_core.config import DocumentParserConfig
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


def test_files_scan_extracts_xlsx_to_markdown_tables(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    workbook = root / "portfolio.xlsx"
    _write_minimal_xlsx(
        workbook,
        rows=[
            ["Company", "Status", "ARR"],
            ["Acme Example", "active", "1200000"],
            ["Widget Co", "watch", "450000"],
        ],
    )
    store = InMemoryKnowledgeStore()

    report = scan_files(store, root=root)

    assert report.ingested == 1
    source = store.source_items[report.source_item_ids[0]]
    assert "## Sheet: Pipeline" in source.content_text
    assert "| Company | Status | ARR |" in source.content_text
    assert "| Acme Example | active | 1200000 |" in source.content_text
    extraction = source.metadata["extra"]["extraction"]
    assert extraction["extractor"] == "xlsx-zip-xml"
    assert extraction["sheet_count"] == 1
    assert extraction["sheets"][0]["name"] == "Pipeline"

    unchanged = scan_files(store, root=root)
    assert unchanged.ingested == 0
    assert unchanged.unchanged_files == 1


def test_files_scan_uses_external_document_parser_for_pdf_tables(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    pdf = root / "annual-report.pdf"
    pdf.write_bytes(b"%PDF-1.4\nnot important for mocked parser")
    parser_payload = {
        "code": "200",
        "status": "success",
        "message": "ok",
        "content": "# Parsed Report\n\n| Metric | Value |\n| --- | --- |\n| Revenue | 1200000 |",
        "json_content": json.dumps(
            {
                "parsing_res_list_merge": [
                    {"block_label": "title", "block_content": "Parsed Report"},
                    {"block_label": "table", "block_content": "Revenue 1200000"},
                ]
            }
        ),
        "trace_id": "trace-123",
    }

    def fake_parser(path: Path, raw: bytes, config: DocumentParserConfig) -> dict[str, object]:
        assert path.name == pdf.name
        assert raw == pdf.read_bytes()
        assert config.return_json is True
        return parser_payload

    monkeypatch.setattr("pska_core.files_connector._call_document_parser_server", fake_parser)
    store = InMemoryKnowledgeStore()

    report = scan_files(
        store,
        root=root,
        document_parser=DocumentParserConfig(
            enabled=True,
            url="http://parser.test/rag/model_parser_file",
            return_json=True,
        ),
    )

    assert report.ingested == 1
    assert report.skipped == []
    source = store.source_items[report.source_item_ids[0]]
    assert "| Metric | Value |" in source.content_text
    extraction = source.metadata["extra"]["extraction"]
    assert extraction["extractor"] == "doc-parser-server"
    assert extraction["trace_id"] == "trace-123"
    assert extraction["json_block_count"] == 2
    assert extraction["json_block_labels"]["table"] == 1


def test_files_scan_accepts_parser_only_image_uploads_when_configured(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    image = root / "scanned-table.png"
    image.write_bytes(b"\x89PNG\r\n")

    def fake_parser(path: Path, raw: bytes, config: DocumentParserConfig) -> dict[str, object]:
        assert path.name == image.name
        assert config.extract_image_content is True
        return {"code": "200", "status": "success", "content": "OCR table text"}

    monkeypatch.setattr("pska_core.files_connector._call_document_parser_server", fake_parser)
    store = InMemoryKnowledgeStore()

    report = scan_files(
        store,
        root=root,
        document_parser=DocumentParserConfig(
            enabled=True,
            url="http://parser.test/rag/model_parser_file",
            extract_image_content=True,
        ),
    )

    assert report.ingested == 1
    source = store.source_items[report.source_item_ids[0]]
    assert source.content_text == "OCR table text"


def test_files_scan_replaces_nul_bytes_from_pdf_text_extraction(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "before\x00after"

    class FakeReader:
        def __init__(self, _path: Path) -> None:
            self.pages = [FakePage()]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=FakeReader))
    root = tmp_path / "notes"
    root.mkdir()
    (root / "nul.pdf").write_bytes(b"%PDF-1.4\n")
    store = InMemoryKnowledgeStore()

    report = scan_files(store, root=root)

    assert report.ingested == 1
    source = store.source_items[report.source_item_ids[0]]
    assert "\x00" not in source.content_text
    assert "before\ufffdafter" in source.content_text
    assert "\x00" not in json.dumps(source.metadata, ensure_ascii=False)


def test_files_scan_uses_configurable_spreadsheet_limits(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    workbook = root / "wide-and-long.xlsx"
    _write_minimal_xlsx(
        workbook,
        rows=[
            ["Company", "Status", "ARR"],
            ["Acme Example", "active", "1200000"],
            ["Widget Co", "watch", "450000"],
        ],
    )
    store = InMemoryKnowledgeStore()

    report = scan_files(
        store,
        root=root,
        spreadsheet_max_rows_per_sheet=2,
        spreadsheet_max_columns=2,
    )

    source = store.source_items[report.source_item_ids[0]]
    assert "| Company | Status |" in source.content_text
    assert "| Acme Example | active |" in source.content_text
    assert "1200000" not in source.content_text
    assert "Widget Co" not in source.content_text
    extraction = source.metadata["extra"]["extraction"]
    assert extraction["row_limit_per_sheet"] == 2
    assert extraction["column_limit"] == 2
    assert extraction["sheets"][0]["truncated_rows"] is True
    assert extraction["sheets"][0]["truncated_columns"] is True


def test_files_scan_reports_xls_optional_dependency(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "xlrd", None)
    root = tmp_path / "notes"
    root.mkdir()
    (root / "legacy.xls").write_bytes(b"not really an xls")

    report = scan_files(InMemoryKnowledgeStore(), root=root)

    assert report.ingested == 0
    assert report.skipped[0]["reason"] == "missing_dependency"
    assert "xlrd required" in report.skipped[0]["detail"]


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


def test_files_scan_reingests_when_manifest_source_item_was_deleted(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "note.txt").write_text("stable content", encoding="utf-8")
    store = InMemoryKnowledgeStore()

    first = scan_files(store, root=root)
    deleted_source_item_id = first.source_item_ids[0]
    del store.source_items[deleted_source_item_id]
    store.source_items_by_hash.clear()

    restored = scan_files(store, root=root)

    assert restored.ingested == 1
    assert restored.new_files == 1
    assert restored.unchanged_files == 0
    assert restored.source_item_ids
    assert restored.connector_state.config["files_manifest"]["note.txt"]["source_item_id"] in store.source_items


def test_files_scan_ingests_marked_directory_as_one_source_with_many_documents(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    novel = root / "novel"
    novel.mkdir(parents=True)
    (novel / ".pska-source.json").write_text(
        json.dumps(
            {
                "type": "source_collection",
                "title": "Novel Project",
                "source_id": "novel-project",
                "documents": ["*.md"],
                "exclude": ["README*.md"],
            }
        ),
        encoding="utf-8",
    )
    (novel / "故事背景.md").write_text("# 背景\n\n世界观。", encoding="utf-8")
    (novel / "正文.md").write_text("# 正文\n\n第一章。", encoding="utf-8")
    (novel / "README_生成器.md").write_text("tooling notes", encoding="utf-8")
    store = InMemoryKnowledgeStore()

    report = scan_files(store, root=root, owner_user_id="user_primary")

    assert report.ingested == 1
    assert len(report.source_item_ids) == 1
    assert report.source_item_ids[0].startswith("src_")
    source = store.source_items[report.source_item_ids[0]]
    assert source.record_type == "file_collection"
    assert source.title == "Novel Project"
    assert source.metadata["collection"] is True
    documents = [document for document in store.documents.values() if document.source_item_id == source.source_item_id]
    assert sorted(document.title for document in documents) == ["故事背景.md", "正文.md"]
    assert len({chunk.source_item_id for chunk in store.chunks.values()}) == 1
    manifest = report.connector_state.config["files_manifest"]
    assert manifest["novel"]["collection"] is True
    assert manifest["novel"]["document_count"] == 2
    unchanged = scan_files(store, root=root, owner_user_id="user_primary")
    assert unchanged.ingested == 0
    assert unchanged.unchanged_files == 1
    assert len(store.source_items) == 1


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


def _write_minimal_xlsx(path: Path, *, rows: list[list[str]]) -> None:
    def cell_name(row_index: int, column_index: int) -> str:
        letters = ""
        index = column_index
        while index:
            index, remainder = divmod(index - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return f"{letters}{row_index}"

    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            cells.append(
                f'<c r="{cell_name(row_index, column_index)}" t="inlineStr">'
                f"<is><t>{_xml_escape(value)}</t></is>"
                "</c>"
            )
        sheet_rows.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Pipeline" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    with zipfile.ZipFile(path, "w") as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
