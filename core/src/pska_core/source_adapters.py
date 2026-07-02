from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from pska_core.config import (
    DEFAULT_FILES_MAX_BYTES,
    DEFAULT_SPREADSHEET_MAX_COLUMNS,
    DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET,
    DocumentParserConfig,
)
from pska_core.connectors import connector_record_to_payload
from pska_core.enums import Visibility
from pska_core.files_connector import scan_files
from pska_core.ingest import IngestService
from pska_core.models import DEFAULT_TENANT_ID, KnowledgeSource, SourceItem
from pska_core.processing import resolve_processing_config
from pska_core.store import KnowledgeStore


FETCH_TIMEOUT_SECONDS = 12
MAX_FEED_ITEMS = 100
MAX_SITEMAP_URLS = 80


@dataclass(slots=True)
class SourceResource:
    resource_id: str
    title: str
    uri: str
    record_type: str
    updated_at: str | None = None
    content_hash: str | None = None
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceAdapterSyncReport:
    root: str
    scanned: int = 0
    ingested: int = 0
    new_files: int = 0
    changed_files: int = 0
    unchanged_files: int = 0
    moved_files: int = 0
    missing_files: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    source_item_ids: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    processing_config: dict[str, Any] = field(default_factory=dict)
    resources: list[dict[str, Any]] = field(default_factory=list)


class SourceAdapter(Protocol):
    source: KnowledgeSource

    def validate(self) -> dict[str, Any]: ...

    def list_resources(self, *, limit: int = 20) -> list[SourceResource]: ...

    def preview(self, *, limit: int = 10) -> dict[str, Any]: ...

    def sync(self, *, limit: int | None = None) -> SourceAdapterSyncReport: ...


class FilesSourceAdapter:
    def __init__(self, store: KnowledgeStore, source: KnowledgeSource, *, embedding_provider: Any = None, processing_config: dict[str, Any] | None = None) -> None:
        self.store = store
        self.source = source
        self.embedding_provider = embedding_provider
        self.processing_config = resolve_processing_config(source.config, processing_config)

    def validate(self) -> dict[str, Any]:
        path = _source_path(self.source)
        return {"ok": path.exists() and path.is_dir(), "path": str(path), "exists": path.exists(), "is_dir": path.is_dir()}

    def list_resources(self, *, limit: int = 20) -> list[SourceResource]:
        path = _source_path(self.source)
        if not path.exists() or not path.is_dir():
            return []
        resources: list[SourceResource] = []
        for child in sorted(path.rglob("*")):
            if len(resources) >= limit:
                break
            if child.is_file():
                resources.append(
                    SourceResource(
                        resource_id=str(child),
                        title=child.name,
                        uri=child.resolve().as_uri(),
                        record_type="file",
                        metadata={"path": str(child), "size_bytes": child.stat().st_size},
                    )
                )
        return resources

    def preview(self, *, limit: int = 10) -> dict[str, Any]:
        validation = self.validate()
        resources = self.list_resources(limit=limit) if validation["ok"] else []
        return {"ok": validation["ok"], "validation": validation, "resources": _resources_payload(resources), "count": len(resources)}

    def sync(self, *, limit: int | None = None) -> SourceAdapterSyncReport:
        path = _source_path(self.source)
        return scan_files(
            self.store,
            root=path,
            owner_user_id=self.source.owner_user_id,
            tenant_id=self.source.tenant_id,
            space_id=self.source.space_id,
            visibility=self.source.visibility,
            visible_team_ids=self.source.visible_team_ids,
            ignore=list(self.source.config.get("ignore") or []),
            max_bytes=int(self.source.config.get("max_bytes") or DEFAULT_FILES_MAX_BYTES),
            spreadsheet_max_rows_per_sheet=int(
                self.source.config.get("spreadsheet_max_rows_per_sheet")
                or self.source.config.get("spreadsheet_row_limit_per_sheet")
                or DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET
            ),
            spreadsheet_max_columns=int(
                self.source.config.get("spreadsheet_max_columns")
                or self.source.config.get("spreadsheet_column_limit")
                or DEFAULT_SPREADSHEET_MAX_COLUMNS
            ),
            document_parser=DocumentParserConfig.from_dict(
                self.source.config.get("document_parser")
                if isinstance(self.source.config.get("document_parser"), dict)
                else None
            ),
            embedding_provider=self.embedding_provider,
            processing_config=self.processing_config,
        )


class RSSAtomSourceAdapter:
    connector_id = "rss"

    def __init__(self, store: KnowledgeStore, source: KnowledgeSource, *, embedding_provider: Any = None, processing_config: dict[str, Any] | None = None) -> None:
        self.store = store
        self.source = source
        self.embedding_provider = embedding_provider
        self.processing_config = resolve_processing_config(source.config, processing_config)

    def validate(self) -> dict[str, Any]:
        url = _source_url(self.source)
        parsed = urlparse(url)
        ok = parsed.scheme in {"http", "https", "file"}
        return {"ok": ok, "url": url, "scheme": parsed.scheme, "adapter": self.connector_id}

    def list_resources(self, *, limit: int = 20) -> list[SourceResource]:
        xml_text = _fetch_text(_source_url(self.source))
        return _parse_feed_resources(xml_text, _source_url(self.source))[: max(0, min(limit, MAX_FEED_ITEMS))]

    def preview(self, *, limit: int = 10) -> dict[str, Any]:
        validation = self.validate()
        if not validation["ok"]:
            return {"ok": False, "validation": validation, "resources": [], "count": 0}
        resources = self.list_resources(limit=limit)
        return {"ok": True, "validation": validation, "resources": _resources_payload(resources), "count": len(resources)}

    def sync(self, *, limit: int | None = None) -> SourceAdapterSyncReport:
        resources = self.list_resources(limit=limit or MAX_FEED_ITEMS)
        return _sync_resources(
            self.store,
            self.source,
            resources,
            connector_id=self.connector_id,
            embedding_provider=self.embedding_provider,
            processing_config=self.processing_config,
        )


class URLPageSourceAdapter:
    connector_id = "url"

    def __init__(self, store: KnowledgeStore, source: KnowledgeSource, *, embedding_provider: Any = None, processing_config: dict[str, Any] | None = None) -> None:
        self.store = store
        self.source = source
        self.embedding_provider = embedding_provider
        self.processing_config = resolve_processing_config(source.config, processing_config)

    def validate(self) -> dict[str, Any]:
        url = _source_url(self.source)
        parsed = urlparse(url)
        ok = parsed.scheme in {"http", "https", "file"}
        return {"ok": ok, "url": url, "scheme": parsed.scheme, "adapter": self.connector_id}

    def list_resources(self, *, limit: int = 20) -> list[SourceResource]:
        url = _source_url(self.source)
        text = _fetch_text(url)
        sitemap_urls = _parse_sitemap_urls(text)
        if sitemap_urls:
            return [
                SourceResource(resource_id=loc, title=_title_from_url(loc), uri=loc, record_type="web_page")
                for loc in sitemap_urls[: max(0, min(limit, MAX_SITEMAP_URLS))]
            ]
        extracted = _extract_html(text, fallback_title=_title_from_url(url))
        content_hash = _content_hash(url, extracted["title"], extracted["text"], "")
        return [
            SourceResource(
                resource_id=url,
                title=extracted["title"],
                uri=url,
                record_type="web_page",
                content_hash=content_hash,
                summary=extracted["text"],
                metadata={"html_title": extracted["title"]},
            )
        ][:limit]

    def preview(self, *, limit: int = 10) -> dict[str, Any]:
        validation = self.validate()
        if not validation["ok"]:
            return {"ok": False, "validation": validation, "resources": [], "count": 0}
        resources = self.list_resources(limit=limit)
        return {"ok": True, "validation": validation, "resources": _resources_payload(resources), "count": len(resources)}

    def sync(self, *, limit: int | None = None) -> SourceAdapterSyncReport:
        listed = self.list_resources(limit=limit or MAX_SITEMAP_URLS)
        resources: list[SourceResource] = []
        failed: list[dict[str, Any]] = []
        for resource in listed:
            if resource.summary:
                resources.append(resource)
                continue
            try:
                text = _fetch_text(resource.uri)
                extracted = _extract_html(text, fallback_title=resource.title or _title_from_url(resource.uri))
                resources.append(
                    SourceResource(
                        resource_id=resource.uri,
                        title=extracted["title"],
                        uri=resource.uri,
                        record_type="web_page",
                        content_hash=_content_hash(resource.uri, extracted["title"], extracted["text"], ""),
                        summary=extracted["text"],
                        metadata={"html_title": extracted["title"], **dict(resource.metadata or {})},
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one URL should not abort a sitemap sync.
                failed.append({"uri": resource.uri, "error": f"{type(exc).__name__}: {exc}"})
        report = _sync_resources(
            self.store,
            self.source,
            resources,
            connector_id=self.connector_id,
            embedding_provider=self.embedding_provider,
            processing_config=self.processing_config,
        )
        report.failed.extend(failed)
        return report


class InlineTextSourceAdapter:
    connector_id = "text"

    def __init__(self, store: KnowledgeStore, source: KnowledgeSource, *, embedding_provider: Any = None, processing_config: dict[str, Any] | None = None) -> None:
        self.store = store
        self.source = source
        self.embedding_provider = embedding_provider
        self.processing_config = resolve_processing_config(source.config, processing_config)

    def validate(self) -> dict[str, Any]:
        text = _inline_text(self.source)
        return {"ok": bool(text.strip()), "adapter": self.connector_id, "chars": len(text), "uri": self.source.uri}

    def list_resources(self, *, limit: int = 20) -> list[SourceResource]:
        if limit <= 0:
            return []
        text = _inline_text(self.source)
        if not text.strip():
            return []
        title = str(self.source.config.get("title") or self.source.name or "Pasted text").strip()
        resource_id = str(self.source.config.get("source_id") or self.source.uri).strip()
        return [
            SourceResource(
                resource_id=resource_id,
                title=title,
                uri=self.source.uri,
                record_type=str(self.source.config.get("record_type") or "pasted_text"),
                content_hash=_content_hash(self.source.uri, title, text, str(self.source.config.get("updated_at") or "")),
                summary=text,
                metadata={
                    "source_type": "text",
                    "input_kind": "pasted_text",
                    **dict(self.source.config.get("metadata") or {}),
                },
            )
        ][:limit]

    def preview(self, *, limit: int = 10) -> dict[str, Any]:
        validation = self.validate()
        resources = self.list_resources(limit=limit) if validation["ok"] else []
        return {"ok": validation["ok"], "validation": validation, "resources": _resources_payload(resources), "count": len(resources)}

    def sync(self, *, limit: int | None = None) -> SourceAdapterSyncReport:
        return _sync_resources(
            self.store,
            self.source,
            self.list_resources(limit=limit or 1),
            connector_id=self.connector_id,
            embedding_provider=self.embedding_provider,
            processing_config=self.processing_config,
        )


class UploadSourceAdapter(InlineTextSourceAdapter):
    connector_id = "upload"

    def list_resources(self, *, limit: int = 20) -> list[SourceResource]:
        resources = super().list_resources(limit=limit)
        for resource in resources:
            resource.record_type = str(self.source.config.get("record_type") or "uploaded_document")
            resource.metadata = {
                "source_type": "upload",
                "input_kind": "uploaded_file",
                "filename": self.source.config.get("filename"),
                "content_type": self.source.config.get("content_type"),
                "size_bytes": self.source.config.get("size_bytes"),
                **dict(self.source.config.get("metadata") or {}),
            }
        return resources


def build_source_adapter(
    store: KnowledgeStore,
    source: KnowledgeSource,
    *,
    embedding_provider: Any = None,
    processing_config: dict[str, Any] | None = None,
) -> SourceAdapter:
    connector_id = str(source.connector_id or source.source_type).lower()
    source_type = str(source.source_type or "").lower()
    if connector_id == "files" or source_type == "folder":
        return FilesSourceAdapter(store, source, embedding_provider=embedding_provider, processing_config=processing_config)
    if connector_id == "rss" or source_type in {"rss", "atom", "feed"}:
        return RSSAtomSourceAdapter(store, source, embedding_provider=embedding_provider, processing_config=processing_config)
    if connector_id == "url" or source_type in {"url", "web", "sitemap"}:
        return URLPageSourceAdapter(store, source, embedding_provider=embedding_provider, processing_config=processing_config)
    if connector_id == "text" or source_type in {"text", "paste", "pasted_text"}:
        return InlineTextSourceAdapter(store, source, embedding_provider=embedding_provider, processing_config=processing_config)
    if connector_id == "upload" or source_type in {"upload", "uploaded_file", "uploaded_document"}:
        return UploadSourceAdapter(store, source, embedding_provider=embedding_provider, processing_config=processing_config)
    raise ValueError(f"Unsupported source adapter: {source.connector_id or source.source_type}")


def supported_source_adapters() -> list[dict[str, Any]]:
    return [
        {"source_type": "folder", "connector_id": "files", "label": "Local folder"},
        {"source_type": "rss", "connector_id": "rss", "label": "RSS/Atom feed"},
        {"source_type": "url", "connector_id": "url", "label": "URL page or sitemap"},
        {"source_type": "text", "connector_id": "text", "label": "Pasted text"},
        {"source_type": "upload", "connector_id": "upload", "label": "Uploaded file"},
    ]


def _sync_resources(
    store: KnowledgeStore,
    source: KnowledgeSource,
    resources: list[SourceResource],
    *,
    connector_id: str,
    embedding_provider: Any = None,
    processing_config: dict[str, Any] | None = None,
) -> SourceAdapterSyncReport:
    report = SourceAdapterSyncReport(root=source.uri, scanned=len(resources), processing_config=resolve_processing_config(source.config, processing_config))
    report.resources = _resources_payload(resources)
    existing = _existing_connector_records(
        store,
        tenant_id=source.tenant_id,
        owner_user_id=source.owner_user_id,
        connector_id=connector_id,
    )
    ingest = IngestService(store, embedding_provider=embedding_provider, processing_config=report.processing_config)
    for resource in resources:
        body = resource.summary.strip()
        if not body:
            report.skipped.append({"uri": resource.uri, "reason": "empty_text"})
            continue
        content_hash = resource.content_hash or _content_hash(resource.uri, resource.title, body, resource.updated_at or "")
        previous = existing.get(resource.resource_id)
        status = "new"
        if previous and previous.content_hash == content_hash:
            report.unchanged_files += 1
            status = "unchanged"
            report.changes.append({"uri": resource.uri, "status": status, "source_item_id": previous.source_item_id})
            continue
        if previous:
            status = "changed"
        payload = connector_record_to_payload(
            {
                "connector_id": connector_id,
                "external_id": resource.resource_id,
                "source_uri": resource.uri,
                "record_type": resource.record_type,
                "title": resource.title,
                "body": body,
                "owner_user_id": source.owner_user_id,
                "tenant_id": source.tenant_id,
                "space_id": source.space_id,
                "visibility": source.visibility.value if isinstance(source.visibility, Visibility) else str(source.visibility),
                "visible_team_ids": source.visible_team_ids,
                "updated_at": resource.updated_at,
                "captured_at": None,
                "permission_metadata": {"source_uri": source.uri, "read_scope": "explicit_url"},
                "scan_cursor": resource.updated_at or resource.uri,
                "content_hash": content_hash,
                "metadata": {"adapter": connector_id, **dict(resource.metadata or {})},
            }
        )
        item = ingest.ingest_channel_payload(payload)
        report.ingested += 1
        report.source_item_ids.append(item.source_item_id)
        if status == "changed":
            report.changed_files += 1
        else:
            report.new_files += 1
        report.changes.append({"uri": resource.uri, "status": status, "source_item_id": item.source_item_id, "content_hash": content_hash})
    return report


def _existing_connector_records(store: KnowledgeStore, *, tenant_id: str, owner_user_id: str, connector_id: str) -> dict[str, SourceItem]:
    existing: dict[str, SourceItem] = {}
    for item in store.list_source_items(tenant_id=tenant_id):
        if item.source_channel != connector_id or item.owner_user_id != owner_user_id:
            continue
        extra = dict(item.metadata.get("extra") or {})
        connector = dict(extra.get("connector") or {})
        external_id = connector.get("external_id")
        if external_id:
            existing[str(external_id)] = item
    return existing


def _parse_feed_resources(xml_text: str, base_url: str) -> list[SourceResource]:
    root = ElementTree.fromstring(xml_text)
    resources: list[SourceResource] = []
    if _local_name(root.tag) == "rss" or root.find("channel") is not None:
        channel = root.find("channel")
        if channel is None:
            channel = root
        for item in channel.findall("item")[:MAX_FEED_ITEMS]:
            title = _child_text(item, "title") or "Untitled feed item"
            link = _child_text(item, "link") or base_url
            guid = _child_text(item, "guid") or link or title
            updated = _child_text(item, "pubDate") or _child_text(item, "updated")
            summary = _strip_html(_child_text(item, "description") or _child_text(item, "encoded") or title)
            resources.append(
                SourceResource(
                    resource_id=guid,
                    title=title,
                    uri=link,
                    record_type="feed_item",
                    updated_at=updated,
                    content_hash=_content_hash(link, title, summary, updated or ""),
                    summary=summary,
                    metadata={"feed_url": base_url},
                )
            )
        return resources
    for entry in root.findall(".//{*}entry")[:MAX_FEED_ITEMS]:
        title = _child_text(entry, "title") or "Untitled feed entry"
        link = _atom_link(entry) or base_url
        entry_id = _child_text(entry, "id") or link or title
        updated = _child_text(entry, "updated") or _child_text(entry, "published")
        summary = _strip_html(_child_text(entry, "summary") or _child_text(entry, "content") or title)
        resources.append(
            SourceResource(
                resource_id=entry_id,
                title=title,
                uri=link,
                record_type="feed_item",
                updated_at=updated,
                content_hash=_content_hash(link, title, summary, updated or ""),
                summary=summary,
                metadata={"feed_url": base_url},
            )
        )
    return resources


def _parse_sitemap_urls(text: str) -> list[str]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []
    if _local_name(root.tag) not in {"urlset", "sitemapindex"}:
        return []
    urls = []
    for loc in root.findall(".//{*}loc"):
        value = (loc.text or "").strip()
        if value:
            urls.append(value)
    return urls


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "PSKA SourceAdapter/1.0"})
    with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310 - user-authorized local knowledge source fetch.
        raw = response.read()
        content_type = response.headers.get("content-type") or ""
    encoding = "utf-8"
    match = re.search(r"charset=([^;]+)", content_type, re.IGNORECASE)
    if match:
        encoding = match.group(1).strip()
    return raw.decode(encoding, errors="replace")


def _extract_html(text: str, *, fallback_title: str) -> dict[str, str]:
    parser = _HTMLTextExtractor()
    parser.feed(text)
    title = parser.title.strip() or fallback_title
    body = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    return {"title": title, "text": body or title}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        if self._in_title:
            self.title = f"{self.title} {value}".strip()
            return
        if self._skip_depth == 0:
            self.text_parts.append(value)


def _strip_html(value: str) -> str:
    return _extract_html(value, fallback_title="")["text"]


def _child_text(element: ElementTree.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return " ".join(part.strip() for part in child.itertext() if part and part.strip()).strip()
    return ""


def _atom_link(entry: ElementTree.Element) -> str:
    fallback = ""
    for link in entry.findall("{*}link"):
        href = link.attrib.get("href") or ""
        rel = link.attrib.get("rel") or "alternate"
        if href and rel == "alternate":
            return href
        if href and not fallback:
            fallback = href
    return fallback


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _content_hash(*parts: str) -> str:
    return "sha256:" + sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _source_url(source: KnowledgeSource) -> str:
    return str(source.config.get("url") or source.uri)


def _inline_text(source: KnowledgeSource) -> str:
    content = source.config.get("content")
    if isinstance(content, dict):
        return str(content.get("text") or content.get("raw_text") or "")
    return str(source.config.get("text") or source.config.get("body") or "")


def _source_path(source: KnowledgeSource) -> Path:
    path = source.config.get("path") or source.permission_scope.get("path")
    if path:
        return Path(str(path)).expanduser()
    return Path(urlparse(source.uri).path).expanduser()


def _resources_payload(resources: list[SourceResource]) -> list[dict[str, Any]]:
    return [
        {
            "resource_id": resource.resource_id,
            "title": resource.title,
            "uri": resource.uri,
            "record_type": resource.record_type,
            "updated_at": resource.updated_at,
            "content_hash": resource.content_hash,
            "summary": resource.summary[:500],
            "metadata": resource.metadata,
        }
        for resource in resources
    ]


def _title_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return name or parsed.netloc or url
