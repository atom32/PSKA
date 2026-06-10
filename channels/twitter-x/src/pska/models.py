from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MediaItem:
    url: str
    kind: str
    local_path: str | None = None
    alt_text: str | None = None


@dataclass(slots=True)
class Comment:
    id: str | None
    url: str | None
    author: str | None
    handle: str | None
    text: str
    created_at: str | None = None
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    source: str = "visible_dom"


@dataclass(slots=True)
class ArchiveRecord:
    id: str
    url: str
    author: str | None = None
    handle: str | None = None
    content: str = ""
    created_at: str | None = None
    likes: int | None = None
    reposts: int | None = None
    views: int | None = None
    images: list[MediaItem] = field(default_factory=list)
    videos: list[MediaItem] = field(default_factory=list)
    quoted_tweet: dict[str, Any] | None = None
    replies: list[Comment] = field(default_factory=list)
    raw_html: str = ""
    screenshot: bytes | None = None
    kind: str = "tweet"

    def metadata(self) -> dict[str, Any]:
        from pska.schema import archive_record_metadata

        return archive_record_metadata(self)
