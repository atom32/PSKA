from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from pska_core.connectors import connector_state_from_mapping, connector_record_to_payload
from pska_core.enums import Visibility
from pska_core.ingest import IngestService
from pska_core.models import ConnectorState, SourceItem
from pska_core.store import KnowledgeStore


TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".csv",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".markdown",
    ".py",
    ".rst",
    ".text",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
DOCUMENT_SUFFIXES = {
    ".docx",
    ".pdf",
}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | DOCUMENT_SUFFIXES
DEFAULT_IGNORE = [".git/**", "**/.git/**", "__pycache__/**", "**/__pycache__/**", ".DS_Store", "**/.DS_Store"]


@dataclass(slots=True)
class FilesScanReport:
    root: str
    connector_state: ConnectorState
    scanned: int = 0
    ingested: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    source_item_ids: list[str] = field(default_factory=list)


def scan_files(
    store: KnowledgeStore,
    *,
    root: Path,
    owner_user_id: str = "user_primary",
    space_id: str = "private_primary",
    visibility: Visibility = Visibility.PRIVATE,
    visible_team_ids: list[str] | None = None,
    ignore: list[str] | None = None,
    max_bytes: int = 1_000_000,
    embedding_provider=None,
) -> FilesScanReport:
    root = root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Files connector root must be an existing directory: {root}")

    state = _connector_state(store, root=root, owner_user_id=owner_user_id)
    if not state.enabled:
        return FilesScanReport(root=str(root), connector_state=state, skipped=[{"root": str(root), "reason": "connector_disabled"}])

    ingest = IngestService(store, embedding_provider=embedding_provider)
    report = FilesScanReport(root=str(root), connector_state=state)
    latest_cursor = state.scan_cursor
    patterns = [*(ignore or []), *DEFAULT_IGNORE]

    for path in _iter_candidate_files(root, ignore=patterns):
        report.scanned += 1
        relative = path.relative_to(root).as_posix()
        try:
            stat = path.stat()
            if stat.st_size > max_bytes:
                report.skipped.append({"path": str(path), "reason": "file_too_large", "size_bytes": stat.st_size})
                continue
            suffix = path.suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                report.skipped.append({"path": str(path), "reason": "unsupported_suffix"})
                continue
            raw = path.read_bytes()
            extracted = _extract_text(path, raw)
            if not extracted["ok"]:
                report.skipped.append({"path": str(path), "reason": extracted["reason"], "detail": extracted.get("detail")})
                continue
            text = str(extracted["text"])
            if not text.strip():
                report.skipped.append({"path": str(path), "reason": "empty_text"})
                continue
            item = _ingest_file(
                ingest,
                path=path,
                root=root,
                relative=relative,
                raw=raw,
                text=text,
                owner_user_id=owner_user_id,
                space_id=space_id,
                visibility=visibility,
                visible_team_ids=visible_team_ids or [],
                stat=stat,
            )
            report.ingested += 1
            report.source_item_ids.append(item.source_item_id)
            latest_cursor = _max_scan_cursor(latest_cursor, str(int(stat.st_mtime_ns)))
        except Exception as exc:  # noqa: BLE001 - per-file failures should not abort a scan.
            report.failed.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

    status = "failed" if report.failed and not report.ingested else "succeeded"
    state.scan_cursor = latest_cursor
    state.sync_status = status
    if report.failed:
        state.last_error = report.failed[0]["error"]
    else:
        state.last_error = None
    report.connector_state = store.upsert_connector_state(state)
    return report


def _extract_text(path: Path, raw: bytes) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return {"ok": True, "text": raw.decode("utf-8", errors="replace"), "extractor": "utf8"}
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - optional dependency.
            return {"ok": False, "reason": "missing_dependency", "detail": f"pypdf required for PDF extraction: {type(exc).__name__}"}
        try:
            reader = PdfReader(path)
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:  # noqa: BLE001 - per-file failure should not abort scan.
            return {"ok": False, "reason": "extract_failed", "detail": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "text": text, "extractor": "pypdf"}
    if suffix == ".docx":
        try:
            from docx import Document  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - optional dependency.
            return {"ok": False, "reason": "missing_dependency", "detail": f"python-docx required for DOCX extraction: {type(exc).__name__}"}
        try:
            document = Document(path)
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "extract_failed", "detail": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "text": text, "extractor": "python-docx"}
    return {"ok": False, "reason": "unsupported_suffix"}


def _connector_state(store: KnowledgeStore, *, root: Path, owner_user_id: str) -> ConnectorState:
    state_id = f"conn_{owner_user_id}_files"
    try:
        state = store.get_connector_state(state_id)
        roots = state.permission_scope.setdefault("roots", [])
        if isinstance(roots, list) and str(root) not in roots:
            roots.append(str(root))
        return state
    except KeyError:
        state = connector_state_from_mapping(
            {
                "connector_id": "files",
                "owner_user_id": owner_user_id,
                "enabled": True,
                "permission_scope": {"roots": [str(root)]},
                "config": {"default_ignore": DEFAULT_IGNORE},
            }
        )
        return store.upsert_connector_state(state)


def _iter_candidate_files(root: Path, *, ignore: list[str]) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(fnmatch(relative, pattern) or fnmatch(path.name, pattern) for pattern in ignore):
            continue
        files.append(path)
    return files


def _ingest_file(
    ingest: IngestService,
    *,
    path: Path,
    root: Path,
    relative: str,
    raw: bytes,
    text: str,
    owner_user_id: str,
    space_id: str,
    visibility: Visibility,
    visible_team_ids: list[str],
    stat,
) -> SourceItem:
    content_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    mime_type = mimetypes.guess_type(path.name)[0] or "text/plain"
    payload = connector_record_to_payload(
        {
            "connector_id": "files",
            "external_id": str(path),
            "source_uri": path.as_uri(),
            "record_type": "file",
            "title": path.name,
            "body": text,
            "owner_user_id": owner_user_id,
            "space_id": space_id,
            "visibility": visibility.value,
            "visible_team_ids": visible_team_ids,
            "updated_at": str(int(stat.st_mtime_ns)),
            "captured_at": None,
            "artifacts": {"path": str(path)},
            "permission_metadata": {"root": str(root), "relative_path": relative, "read_scope": "explicit_directory"},
            "scan_cursor": str(int(stat.st_mtime_ns)),
            "content_hash": content_hash,
            "metadata": {"mime_type": mime_type, "size_bytes": stat.st_size},
        }
    )
    return ingest.ingest_channel_payload(payload)


def _max_scan_cursor(current: str | None, candidate: str) -> str:
    if not current:
        return candidate
    try:
        return str(max(int(current), int(candidate)))
    except ValueError:
        return candidate
