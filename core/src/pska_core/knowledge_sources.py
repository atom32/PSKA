from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pska_core.config import PSKAConfig
from pska_core.enums import Visibility
from pska_core.models import KnowledgeSource, SyncRun, utc_now
from pska_core.serde import to_jsonable
from pska_core.store import KnowledgeStore


class KnowledgeSourceService:
    """User-facing lifecycle service for PSKA knowledge sources."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def list_sources(
        self,
        *,
        owner_user_id: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[KnowledgeSource]:
        return self.store.list_knowledge_sources(owner_user_id=owner_user_id, source_type=source_type, status=status)

    def add_folder_source(
        self,
        path: Path,
        *,
        owner_user_id: str = "user_primary",
        name: str | None = None,
        mode: str = "manual",
        status: str = "authorized",
        space_id: str = "private_primary",
        visibility: Visibility = Visibility.PRIVATE,
        visible_team_ids: list[str] | None = None,
        ignore: list[str] | None = None,
        max_bytes: int = 1_000_000,
    ) -> KnowledgeSource:
        root = path.expanduser().resolve()
        source = KnowledgeSource(
            knowledge_source_id=knowledge_source_id(owner_user_id, root.as_uri()),
            owner_user_id=owner_user_id,
            name=name or root.name or str(root),
            source_type="folder",
            uri=root.as_uri(),
            mode=mode,
            status=status,
            connector_id="files",
            space_id=space_id,
            visibility=visibility,
            visible_team_ids=visible_team_ids or [],
            permission_scope={"path": str(root), "read_scope": "explicit_directory"},
            config={"path": str(root), "ignore": ignore or [], "max_bytes": max_bytes},
        )
        return self.store.upsert_knowledge_source(source)

    def seed_from_config(self, config: PSKAConfig) -> list[KnowledgeSource]:
        existing_by_uri = {
            source.uri: source
            for source in self.store.list_knowledge_sources(owner_user_id=config.files.owner_user_id)
        }
        seeded = []
        for root in config.files.roots:
            resolved = root.expanduser().resolve()
            existing = existing_by_uri.get(resolved.as_uri())
            source = self.add_folder_source(
                resolved,
                owner_user_id=config.files.owner_user_id,
                space_id=config.files.space_id,
                visibility=Visibility(config.files.visibility),
                ignore=list(config.files.ignore),
                max_bytes=config.files.max_bytes,
                status=existing.status if existing is not None else "authorized",
            )
            if existing is None or _folder_source_differs(existing, source):
                seeded.append(source)
        return seeded

    def source_path(self, source: KnowledgeSource) -> Path:
        path = source.config.get("path") or source.permission_scope.get("path")
        if path:
            return Path(str(path)).expanduser()
        if source.uri.startswith("file://"):
            from urllib.parse import unquote, urlparse

            parsed = urlparse(source.uri)
            return Path(unquote(parsed.path)).expanduser()
        return Path(source.uri).expanduser()

    def record_sync_report(self, source: KnowledgeSource, report: Any, *, error: str | None = None) -> SyncRun:
        failed_count = len(getattr(report, "failed", []) or [])
        status = "failed" if error or failed_count else "succeeded"
        report_error = error
        if report_error is None and failed_count:
            report_error = (getattr(report, "failed", []) or [{}])[0].get("error")
        run = SyncRun(
            sync_run_id=f"sync_{uuid4().hex}",
            knowledge_source_id=source.knowledge_source_id,
            owner_user_id=source.owner_user_id,
            connector_id=source.connector_id,
            status=status,
            started_at=utc_now(),
            finished_at=utc_now(),
            scanned=int(getattr(report, "scanned", 0) or 0),
            ingested=int(getattr(report, "ingested", 0) or 0),
            new_files=int(getattr(report, "new_files", 0) or 0),
            changed_files=int(getattr(report, "changed_files", 0) or 0),
            unchanged_files=int(getattr(report, "unchanged_files", 0) or 0),
            moved_files=int(getattr(report, "moved_files", 0) or 0),
            missing_files=int(getattr(report, "missing_files", 0) or 0),
            skipped=len(getattr(report, "skipped", []) or []),
            failed=failed_count,
            error=report_error,
            report=to_jsonable(report),
        )
        return self.store.add_sync_run(run)

    def record_sync_error(self, source: KnowledgeSource, error: str) -> SyncRun:
        run = SyncRun(
            sync_run_id=f"sync_{uuid4().hex}",
            knowledge_source_id=source.knowledge_source_id,
            owner_user_id=source.owner_user_id,
            connector_id=source.connector_id,
            status="failed",
            started_at=utc_now(),
            finished_at=utc_now(),
            failed=1,
            error=error,
            report={"error": error},
        )
        return self.store.add_sync_run(run)


def knowledge_source_id(owner_user_id: str, uri: str) -> str:
    return f"ks_{uuid5(NAMESPACE_URL, f'{owner_user_id}:{uri}').hex}"


def _folder_source_differs(existing: KnowledgeSource, desired: KnowledgeSource) -> bool:
    return any(
        [
            existing.name != desired.name,
            existing.source_type != desired.source_type,
            existing.mode != desired.mode,
            existing.status != desired.status,
            existing.connector_id != desired.connector_id,
            existing.space_id != desired.space_id,
            existing.visibility != desired.visibility,
            list(existing.visible_team_ids or []) != list(desired.visible_team_ids or []),
            dict(existing.permission_scope or {}) != dict(desired.permission_scope or {}),
            dict(existing.config or {}) != dict(desired.config or {}),
        ]
    )
