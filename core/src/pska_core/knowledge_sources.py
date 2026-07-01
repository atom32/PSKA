from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid4, uuid5

from pska_core.config import DEFAULT_FILES_MAX_BYTES, DEFAULT_SPREADSHEET_MAX_COLUMNS, DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET, PSKAConfig
from pska_core.enums import Visibility
from pska_core.models import DEFAULT_TENANT_ID, KnowledgeSource, ProcessingSpan, SyncRun, utc_now
from pska_core.serde import to_jsonable
from pska_core.store import KnowledgeStore


class KnowledgeSourceService:
    """User-facing lifecycle service for PSKA knowledge sources."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def list_sources(
        self,
        *,
        tenant_id: str | None = None,
        owner_user_id: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[KnowledgeSource]:
        return self.store.list_knowledge_sources(tenant_id=tenant_id, owner_user_id=owner_user_id, source_type=source_type, status=status)

    def add_folder_source(
        self,
        path: Path,
        *,
        owner_user_id: str = "user_primary",
        tenant_id: str = DEFAULT_TENANT_ID,
        name: str | None = None,
        mode: str = "manual",
        status: str = "authorized",
        space_id: str = "private_primary",
        visibility: Visibility = Visibility.PRIVATE,
        visible_team_ids: list[str] | None = None,
        ignore: list[str] | None = None,
        max_bytes: int = DEFAULT_FILES_MAX_BYTES,
        spreadsheet_max_rows_per_sheet: int = DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET,
        spreadsheet_max_columns: int = DEFAULT_SPREADSHEET_MAX_COLUMNS,
    ) -> KnowledgeSource:
        root = path.expanduser().resolve()
        source = KnowledgeSource(
            knowledge_source_id=knowledge_source_id(owner_user_id, root.as_uri(), tenant_id=tenant_id),
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
            config={
                "path": str(root),
                "ignore": ignore or [],
                "max_bytes": max_bytes,
                "spreadsheet_max_rows_per_sheet": spreadsheet_max_rows_per_sheet,
                "spreadsheet_max_columns": spreadsheet_max_columns,
            },
            tenant_id=tenant_id,
        )
        return self.store.upsert_knowledge_source(source)

    def add_rss_source(
        self,
        url: str,
        *,
        owner_user_id: str = "user_primary",
        tenant_id: str = DEFAULT_TENANT_ID,
        name: str | None = None,
        mode: str = "manual",
        status: str = "authorized",
        space_id: str = "private_primary",
        visibility: Visibility = Visibility.PRIVATE,
        visible_team_ids: list[str] | None = None,
        processing_config: dict[str, Any] | None = None,
    ) -> KnowledgeSource:
        return self._add_url_like_source(
            url,
            source_type="rss",
            connector_id="rss",
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            name=name,
            mode=mode,
            status=status,
            space_id=space_id,
            visibility=visibility,
            visible_team_ids=visible_team_ids,
            processing_config=processing_config,
        )

    def add_url_source(
        self,
        url: str,
        *,
        owner_user_id: str = "user_primary",
        tenant_id: str = DEFAULT_TENANT_ID,
        name: str | None = None,
        mode: str = "manual",
        status: str = "authorized",
        space_id: str = "private_primary",
        visibility: Visibility = Visibility.PRIVATE,
        visible_team_ids: list[str] | None = None,
        processing_config: dict[str, Any] | None = None,
    ) -> KnowledgeSource:
        return self._add_url_like_source(
            url,
            source_type="url",
            connector_id="url",
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            name=name,
            mode=mode,
            status=status,
            space_id=space_id,
            visibility=visibility,
            visible_team_ids=visible_team_ids,
            processing_config=processing_config,
        )

    def _add_url_like_source(
        self,
        url: str,
        *,
        source_type: str,
        connector_id: str,
        owner_user_id: str,
        tenant_id: str,
        name: str | None,
        mode: str,
        status: str,
        space_id: str,
        visibility: Visibility,
        visible_team_ids: list[str] | None,
        processing_config: dict[str, Any] | None,
    ) -> KnowledgeSource:
        uri = str(url).strip()
        parsed = urlparse(uri)
        if parsed.scheme not in {"http", "https", "file"}:
            raise ValueError(f"{source_type} source requires http(s) URL")
        config: dict[str, Any] = {"url": uri}
        if processing_config:
            config["processing"] = processing_config
        source = KnowledgeSource(
            knowledge_source_id=knowledge_source_id(owner_user_id, uri, tenant_id=tenant_id),
            owner_user_id=owner_user_id,
            name=name or _url_source_name(uri),
            source_type=source_type,
            uri=uri,
            mode=mode,
            status=status,
            connector_id=connector_id,
            space_id=space_id,
            visibility=visibility,
            visible_team_ids=visible_team_ids or [],
            permission_scope={"url": uri, "read_scope": "explicit_url"},
            config=config,
            tenant_id=tenant_id,
        )
        return self.store.upsert_knowledge_source(source)

    def seed_from_config(self, config: PSKAConfig) -> list[KnowledgeSource]:
        existing_by_uri = {
            source.uri: source
            for source in self.store.list_knowledge_sources(tenant_id=config.files.tenant_id, owner_user_id=config.files.owner_user_id)
        }
        seeded = []
        for root in config.files.roots:
            resolved = root.expanduser().resolve()
            existing = existing_by_uri.get(resolved.as_uri())
            source = self.add_folder_source(
                resolved,
                owner_user_id=config.files.owner_user_id,
                tenant_id=config.files.tenant_id,
                space_id=config.files.space_id,
                visibility=Visibility(config.files.visibility),
                ignore=list(config.files.ignore),
                max_bytes=config.files.max_bytes,
                spreadsheet_max_rows_per_sheet=config.files.spreadsheet_max_rows_per_sheet,
                spreadsheet_max_columns=config.files.spreadsheet_max_columns,
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
        report_payload = to_jsonable(report)
        if isinstance(report_payload, dict) and "effective_processing_config" not in report_payload:
            processing_config = report_payload.get("processing_config")
            if processing_config:
                report_payload["effective_processing_config"] = processing_config
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
            report=report_payload if isinstance(report_payload, dict) else {"report": report_payload},
            tenant_id=source.tenant_id,
        )
        stored = self.store.add_sync_run(run)
        _record_processing_spans(self.store, source, stored, report)
        return stored

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
            tenant_id=source.tenant_id,
        )
        stored = self.store.add_sync_run(run)
        _record_processing_spans(self.store, source, stored, None)
        return stored


def knowledge_source_id(owner_user_id: str, uri: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return f"ks_{uuid5(NAMESPACE_URL, f'{tenant_id}:{owner_user_id}:{uri}').hex}"


def _url_source_name(url: str) -> str:
    parsed = urlparse(url)
    path_name = Path(parsed.path).name
    return path_name or parsed.netloc or url


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


def _record_processing_spans(store: KnowledgeStore, source: KnowledgeSource, run: SyncRun, report: Any | None) -> None:
    source_item_ids = list(getattr(report, "source_item_ids", []) or [])
    changes = list(getattr(report, "changes", []) or [])
    failed = list(getattr(report, "failed", []) or [])
    skipped = list(getattr(report, "skipped", []) or [])
    processing_config = _processing_config_from_report(report)
    root = getattr(report, "root", None) or source.config.get("path") or source.uri
    base_output = {
        "scanned": run.scanned,
        "ingested": run.ingested,
        "new_files": run.new_files,
        "changed_files": run.changed_files,
        "unchanged_files": run.unchanged_files,
        "moved_files": run.moved_files,
        "missing_files": run.missing_files,
        "skipped": run.skipped,
        "failed": run.failed,
    }
    stage_payloads = [
        (
            "discover",
            "failed" if run.status == "failed" and not source_item_ids else "succeeded",
            {"root": root, "connector_id": source.connector_id},
            {**base_output, "change_count": len(changes)},
            {"changes": changes[:50], "skipped": skipped[:25]},
            run.error,
        ),
        (
            "extract",
            "failed" if failed and not source_item_ids else ("succeeded" if source_item_ids else "skipped"),
            {"root": root},
            {"source_item_ids": source_item_ids, "failed": failed[:25], "skipped": skipped[:25]},
            {},
            run.error if failed and not source_item_ids else None,
        ),
        (
            "chunk",
            "succeeded" if source_item_ids else "skipped",
            {"source_item_ids": source_item_ids},
            {"source_item_count": len(source_item_ids)},
            {"chunking": dict(processing_config.get("chunking") or {})},
            None,
        ),
        (
            "embed",
            "succeeded" if source_item_ids else "skipped",
            {"source_item_ids": source_item_ids},
            {"source_item_count": len(source_item_ids)},
            {},
            None,
        ),
        (
            "index",
            "failed" if run.status == "failed" else ("succeeded" if source_item_ids or run.scanned else "skipped"),
            {"source_item_ids": source_item_ids},
            {**base_output, "source_item_count": len(source_item_ids)},
            {},
            run.error if run.status == "failed" else None,
        ),
        (
            "digest",
            _digest_span_status(processing_config, source_item_ids),
            {"source_item_ids": source_item_ids},
            {"source_item_count": len(source_item_ids)},
            {"digest": dict(processing_config.get("digest") or {})},
            None,
        ),
    ]
    for stage, status, input_payload, output_payload, metadata, error in stage_payloads:
        span = ProcessingSpan(
            processing_span_id=f"pspan_{uuid5(NAMESPACE_URL, f'{run.sync_run_id}:{stage}').hex}",
            knowledge_source_id=source.knowledge_source_id,
            owner_user_id=source.owner_user_id,
            stage=stage,
            status=status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            sync_run_id=run.sync_run_id,
            source_item_id=None,
            duration_ms=_duration_ms(run),
            input=input_payload,
            output=output_payload,
            metadata=metadata,
            error=error,
            tenant_id=source.tenant_id,
        )
        store.add_processing_span(span)


def _processing_config_from_report(report: Any | None) -> dict[str, Any]:
    if report is None:
        return {}
    value = getattr(report, "processing_config", None)
    return dict(value) if isinstance(value, dict) else {}


def _digest_span_status(processing_config: dict[str, Any], source_item_ids: list[str]) -> str:
    digest_config = dict(processing_config.get("digest") or {})
    if digest_config.get("enabled", True) is False:
        return "skipped"
    return "pending" if source_item_ids else "skipped"


def _duration_ms(run: SyncRun) -> int | None:
    if not run.finished_at:
        return None
    return max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))
