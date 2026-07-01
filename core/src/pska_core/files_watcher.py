from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from pathlib import Path
from typing import Callable

from pska_core.config import DEFAULT_FILES_MAX_BYTES, DEFAULT_SPREADSHEET_MAX_COLUMNS, DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET
from pska_core.enums import Visibility
from pska_core.files_connector import FilesScanReport, scan_files
from pska_core.models import DEFAULT_TENANT_ID
from pska_core.store import KnowledgeStore


@dataclass(slots=True)
class FilesWatchSummary:
    roots: list[str]
    scans: int = 0
    events: int = 0
    reports: list[FilesScanReport] = field(default_factory=list)


def watch_files(
    store: KnowledgeStore,
    *,
    roots: list[Path],
    owner_user_id: str = "user_primary",
    tenant_id: str = DEFAULT_TENANT_ID,
    space_id: str = "private_primary",
    visibility: Visibility = Visibility.PRIVATE,
    ignore: list[str] | None = None,
    max_bytes: int = DEFAULT_FILES_MAX_BYTES,
    spreadsheet_max_rows_per_sheet: int = DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET,
    spreadsheet_max_columns: int = DEFAULT_SPREADSHEET_MAX_COLUMNS,
    debounce_seconds: float = 2.0,
    initial_sync: bool = False,
    max_events: int = 0,
    on_report: Callable[[FilesScanReport], None] | None = None,
    embedding_provider=None,
) -> FilesWatchSummary:
    try:
        from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]
        from watchdog.observers import Observer  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - optional dependency.
        raise RuntimeError("watchdog is required for files-watch. Install pska-core[watch].") from exc

    resolved_roots = [root.expanduser().resolve() for root in roots]
    if not resolved_roots:
        raise ValueError("files-watch requires at least one root")
    for root in resolved_roots:
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Files connector root must be an existing directory: {root}")

    summary = FilesWatchSummary(roots=[str(root) for root in resolved_roots])
    pending: set[Path] = set()
    last_event_at = 0.0
    lock = threading.Lock()

    def sync_root(root: Path) -> None:
        report = scan_files(
            store,
            root=root,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            space_id=space_id,
            visibility=visibility,
            ignore=ignore or [],
            max_bytes=max_bytes,
            spreadsheet_max_rows_per_sheet=spreadsheet_max_rows_per_sheet,
            spreadsheet_max_columns=spreadsheet_max_columns,
            embedding_provider=embedding_provider,
        )
        summary.scans += 1
        summary.reports.append(report)
        if on_report:
            on_report(report)

    class Handler(FileSystemEventHandler):  # type: ignore[misc,valid-type]
        def __init__(self, root: Path) -> None:
            self.root = root

        def on_any_event(self, event) -> None:  # noqa: ANN001 - watchdog event type is optional.
            nonlocal last_event_at
            if getattr(event, "is_directory", False):
                return
            with lock:
                pending.add(self.root)
                last_event_at = time.monotonic()
                summary.events += 1

    if initial_sync:
        for root in resolved_roots:
            sync_root(root)

    observer = Observer()
    for root in resolved_roots:
        observer.schedule(Handler(root), str(root), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(0.2)
            with lock:
                ready = bool(pending) and time.monotonic() - last_event_at >= debounce_seconds
                roots_to_sync = sorted(pending) if ready else []
                if ready:
                    pending.clear()
            for root in roots_to_sync:
                sync_root(root)
            if max_events and summary.events >= max_events and not pending:
                break
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=5)
    return summary
