from __future__ import annotations

from pathlib import Path
from typing import Any

from pska_core.enums import Visibility
from pska_core.models import ChannelIngestPayload


def archive_metadata_to_payload(
    metadata: dict[str, Any],
    *,
    owner_user_id: str,
    space_id: str,
    visibility: Visibility = Visibility.PRIVATE,
    visible_team_ids: list[str] | None = None,
    archive_dir: Path | None = None,
) -> ChannelIngestPayload:
    """Convert Twitter/X archive metadata.json to Core ingest payload v1."""

    if metadata.get("schema_version") == "pska.archive.v2":
        return _v2_metadata_to_payload(
            metadata,
            owner_user_id=owner_user_id,
            space_id=space_id,
            visibility=visibility,
            visible_team_ids=visible_team_ids,
            archive_dir=archive_dir,
        )

    if _is_legacy_twitter_zip(metadata):
        metadata = _legacy_metadata_to_v1_shape(metadata)

    source = metadata.get("source") or "twitter"
    record_id = str(metadata.get("source_id") or metadata["id"])
    content = dict(metadata.get("content") or {})
    raw_paths = {}
    if archive_dir:
        for filename in ["metadata.json", "content.md", "comments.json", "raw.html", "screenshot.png"]:
            path = archive_dir / filename
            if path.exists():
                raw_paths[filename] = str(path)
    return ChannelIngestPayload(
        schema_version="pska.channel_ingest.v1",
        source_channel=str(source),
        record_type=str(metadata.get("record_type") or "tweet"),
        source_id=record_id,
        url=metadata.get("url"),
        title=(content.get("text") or record_id)[:80],
        author=dict(metadata.get("author") or {}),
        content=content,
        created_at=metadata.get("created_at"),
        captured_at=metadata.get("captured_at"),
        media=list(metadata.get("media") or []),
        raw_paths=raw_paths,
        owner_user_id=owner_user_id,
        space_id=space_id,
        visibility=visibility,
        visible_team_ids=visible_team_ids or [],
        extra={
            "comments": metadata.get("comments") or [],
            "metrics": metadata.get("metrics") or {},
            "archive_schema_version": metadata.get("schema_version"),
            **dict(metadata.get("extra") or {}),
        },
    )


def _v2_metadata_to_payload(
    metadata: dict[str, Any],
    *,
    owner_user_id: str,
    space_id: str,
    visibility: Visibility,
    visible_team_ids: list[str] | None = None,
    archive_dir: Path | None = None,
) -> ChannelIngestPayload:
    pska = metadata.get("pska") or {}
    content = dict(metadata.get("content") or {})
    raw_paths = dict(metadata.get("artifacts") or {})
    if archive_dir:
        raw_paths = {
            key: str(archive_dir / value)
            for key, value in raw_paths.items()
            if isinstance(value, str) and value and not value.endswith("/")
        }
    return ChannelIngestPayload(
        schema_version="pska.channel_ingest.v1",
        source_channel=str(metadata.get("source") or "twitter"),
        record_type=str(metadata.get("record_type") or "tweet"),
        source_id=str(metadata["source_id"]),
        url=metadata.get("canonical_url") or metadata.get("url"),
        title=(content.get("text") or metadata.get("source_id") or "")[:80],
        author=dict(metadata.get("author") or {}),
        content=content,
        created_at=metadata.get("created_at"),
        captured_at=metadata.get("captured_at"),
        media=list(metadata.get("media") or []),
        raw_paths=raw_paths,
        owner_user_id=owner_user_id or pska.get("owner_user_id") or "user_primary",
        space_id=space_id or pska.get("space_id") or "private_primary",
        visibility=visibility or Visibility(pska.get("visibility") or Visibility.PRIVATE),
        visible_team_ids=visible_team_ids if visible_team_ids is not None else list(pska.get("visible_team_ids") or []),
        extra={
            "comments": metadata.get("comments") or [],
            "metrics": metadata.get("metrics") or {},
            "archive_schema_version": metadata.get("schema_version"),
            "capture": metadata.get("capture") or {},
            "extraction": metadata.get("extraction") or {},
            "quoted_items": metadata.get("quoted_items") or [],
            **dict(metadata.get("extra") or {}),
        },
    )


def _is_legacy_twitter_zip(metadata: dict[str, Any]) -> bool:
    return isinstance(metadata.get("content"), str) and "images" in metadata


def _legacy_metadata_to_v1_shape(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "legacy.twitter_zip",
        "source": "twitter",
        "record_type": metadata.get("record_type") or "tweet",
        "id": metadata.get("id"),
        "url": metadata.get("url"),
        "author": {"name": metadata.get("author"), "handle": metadata.get("handle")},
        "content": {"text": metadata.get("content") or ""},
        "created_at": metadata.get("created_at"),
        "captured_at": metadata.get("captured_at"),
        "media": [
            *[
                {"kind": "image", "url": url, "local_path": None, "download_status": "remote_only"}
                for url in metadata.get("images", [])
            ],
            *[
                {"kind": "video", "url": url, "local_path": None, "download_status": "remote_only"}
                for url in metadata.get("videos", [])
            ],
        ],
        "comments": [],
        "metrics": {},
        "extra": {"legacy_schema_version": "legacy.twitter_zip"},
    }
