from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pska.models import ArchiveRecord, Comment, MediaItem

SCHEMA_VERSION = "pska.archive.v2"
LEGACY_SCHEMA_VERSION = "pska.archive.v1"


def media_item(
    *,
    kind: str,
    url: str,
    local_path: str | None = None,
    alt_text: str | None = None,
    content_type: str | None = None,
    download_status: str = "ok",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "url": url,
        "local_path": local_path,
        "alt_text": alt_text,
        "content_type": content_type,
        "download_status": download_status,
    }


def comment_item(comment: Comment | dict[str, Any]) -> dict[str, Any]:
    data = asdict(comment) if isinstance(comment, Comment) else dict(comment)
    return {
        "id": data.get("id"),
        "url": data.get("url"),
        "author": {
            "name": data.get("author"),
            "handle": data.get("handle"),
        },
        "content": {
            "text": data.get("text") or "",
            "raw_text": data.get("raw_text") or "",
        },
        "created_at": data.get("created_at"),
        "media": [
            *[media_item(kind="image", url=url, download_status="remote_only") for url in data.get("images", [])],
            *[media_item(kind="video", url=url, download_status="remote_only") for url in data.get("videos", [])],
        ],
        "metrics": data.get("metrics") or {},
        "source": data.get("source") or "visible_dom",
    }


def record_metadata(
    *,
    record_id: str,
    url: str,
    source: str = "twitter",
    record_type: str = "tweet",
    author_name: str | None = None,
    author_handle: str | None = None,
    text: str = "",
    created_at: str | None = None,
    captured_at: str | None = None,
    images: list[MediaItem | dict[str, Any] | str] | None = None,
    videos: list[MediaItem | dict[str, Any] | str] | None = None,
    comments: list[Comment | dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    raw_text: str = "",
    canonical_url: str | None = None,
    owner_user_id: str = "user_primary",
    space_id: str = "private_primary",
    visibility: str = "private",
    visible_team_ids: list[str] | None = None,
    capture: dict[str, Any] | None = None,
    artifacts: dict[str, str] | None = None,
    extraction: dict[str, Any] | None = None,
    quoted_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "record_type": record_type,
        "source_id": record_id,
        "url": url,
        "canonical_url": canonical_url or url,
        "author": {
            "name": author_name,
            "handle": author_handle,
            "profile_url": _profile_url(author_handle),
        },
        "content": {
            "text": text,
            "raw_text": raw_text,
            "language": None,
        },
        "created_at": created_at,
        "captured_at": captured_at,
        "capture": {
            "method": "python_cli",
            "extension_version": None,
            "page_url": url,
            "visible_comment_limit": None,
            **(capture or {}),
        },
        "media": [
            *[_coerce_media(item, "image") for item in images or []],
            *[_coerce_media(item, "video") for item in videos or []],
        ],
        "comments": [comment_item(comment) for comment in comments or []],
        "quoted_items": quoted_items or [],
        "metrics": metrics or {},
        "artifacts": artifacts
        or {
            "metadata": "metadata.json",
            "markdown": "content.md",
            "comments": "comments.json",
            "raw_html": "raw.html",
            "screenshot": "screenshot.png",
            "media_dir": "media/",
        },
        "pska": {
            "owner_user_id": owner_user_id,
            "space_id": space_id,
            "visibility": visibility,
            "visible_team_ids": visible_team_ids or [],
        },
        "extraction": extraction or {"status": "ok", "warnings": [], "source": "visible_dom"},
        "extra": extra or {},
    }


def archive_record_metadata(record: ArchiveRecord) -> dict[str, Any]:
    metrics = {
        "likes": record.likes,
        "reposts": record.reposts,
        "views": record.views,
    }
    return record_metadata(
        record_id=record.id,
        url=record.url,
        record_type=getattr(record, "kind", "tweet"),
        author_name=record.author,
        author_handle=record.handle,
        text=record.content,
        created_at=record.created_at,
        images=record.images,
        videos=record.videos,
        comments=record.replies,
        metrics={key: value for key, value in metrics.items() if value is not None},
        raw_text=record.raw_html,
        quoted_items=[record.quoted_tweet] if record.quoted_tweet else [],
        extra={"legacy_id": record.id},
    )


def _coerce_media(item: MediaItem | dict[str, Any] | str, default_kind: str) -> dict[str, Any]:
    if isinstance(item, str):
        return media_item(kind=default_kind, url=item, download_status="remote_only")
    data = asdict(item) if isinstance(item, MediaItem) else dict(item)
    return media_item(
        kind=data.get("kind") or default_kind,
        url=data.get("url") or "",
        local_path=data.get("local_path"),
        alt_text=data.get("alt_text"),
        content_type=data.get("content_type"),
        download_status=data.get("download_status") or ("ok" if data.get("local_path") else "remote_only"),
    )


def pska_payload_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    pska = metadata.get("pska") or {}
    return {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": metadata.get("source") or "twitter",
        "record_type": metadata.get("record_type") or "tweet",
        "source_id": metadata.get("source_id") or metadata.get("id"),
        "url": metadata.get("canonical_url") or metadata.get("url"),
        "title": ((metadata.get("content") or {}).get("text") or metadata.get("source_id") or "")[:80],
        "author": metadata.get("author") or {},
        "content": metadata.get("content") or {},
        "created_at": metadata.get("created_at"),
        "captured_at": metadata.get("captured_at"),
        "media": metadata.get("media") or [],
        "raw_paths": metadata.get("artifacts") or {},
        "owner_user_id": pska.get("owner_user_id") or "user_primary",
        "space_id": pska.get("space_id") or "private_primary",
        "visibility": pska.get("visibility") or "private",
        "visible_team_ids": pska.get("visible_team_ids") or [],
        "extra": {
            "comments": metadata.get("comments") or [],
            "metrics": metadata.get("metrics") or {},
            "archive_schema_version": metadata.get("schema_version"),
            "capture": metadata.get("capture") or {},
            "extraction": metadata.get("extraction") or {},
            "quoted_items": metadata.get("quoted_items") or [],
            **dict(metadata.get("extra") or {}),
        },
    }


def _profile_url(handle: str | None) -> str | None:
    if not handle:
        return None
    return f"https://x.com/{handle.lstrip('@')}"
