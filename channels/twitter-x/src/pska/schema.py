from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pska.models import ArchiveRecord, Comment, MediaItem

SCHEMA_VERSION = "pska.archive.v1"


def media_item(
    *,
    kind: str,
    url: str,
    local_path: str | None = None,
    alt_text: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "url": url,
        "local_path": local_path,
        "alt_text": alt_text,
        "content_type": content_type,
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
            *[media_item(kind="image", url=url) for url in data.get("images", [])],
            *[media_item(kind="video", url=url) for url in data.get("videos", [])],
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
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "record_type": record_type,
        "id": record_id,
        "url": url,
        "author": {
            "name": author_name,
            "handle": author_handle,
        },
        "content": {
            "text": text,
        },
        "created_at": created_at,
        "captured_at": captured_at,
        "media": [
            *[_coerce_media(item, "image") for item in images or []],
            *[_coerce_media(item, "video") for item in videos or []],
        ],
        "comments": [comment_item(comment) for comment in comments or []],
        "metrics": metrics or {},
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
        extra={"quoted_tweet": record.quoted_tweet} if record.quoted_tweet else {},
    )


def _coerce_media(item: MediaItem | dict[str, Any] | str, default_kind: str) -> dict[str, Any]:
    if isinstance(item, str):
        return media_item(kind=default_kind, url=item)
    data = asdict(item) if isinstance(item, MediaItem) else dict(item)
    return media_item(
        kind=data.get("kind") or default_kind,
        url=data.get("url") or "",
        local_path=data.get("local_path"),
        alt_text=data.get("alt_text"),
        content_type=data.get("content_type"),
    )
