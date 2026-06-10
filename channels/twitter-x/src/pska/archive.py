from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pska.models import ArchiveRecord, MediaItem
from pska.schema import comment_item, pska_payload_from_metadata


class ArchiveBuilder:
    def __init__(self, archive_root: Path) -> None:
        self.archive_root = archive_root

    def write(self, record: ArchiveRecord) -> Path:
        tweet_dir = self.archive_root / "twitter" / record.id
        media_dir = tweet_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        self._download_images(record.images, media_dir)
        (tweet_dir / "raw.html").write_text(record.raw_html, encoding="utf-8")
        if record.screenshot:
            (tweet_dir / "screenshot.png").write_bytes(record.screenshot)
        else:
            (tweet_dir / "screenshot.png").write_bytes(b"")
        (tweet_dir / "content.md").write_text(self.to_markdown(record), encoding="utf-8")
        comments = [comment_item(comment) for comment in record.replies]
        (tweet_dir / "comments.json").write_text(json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
        metadata = record.metadata()
        (tweet_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (tweet_dir / "pska_payload.json").write_text(
            json.dumps(pska_payload_from_metadata(metadata), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return tweet_dir

    def to_markdown(self, record: ArchiveRecord) -> str:
        lines = [
            "# X Article" if record.kind == "x_article" else "# Tweet",
            "",
            f"ID: {record.id}",
            f"Author: {record.author or ''}",
            f"Handle: {record.handle or ''}",
            f"Date: {record.created_at or ''}",
            "",
            "URL:",
            record.url,
            "",
            "---",
            "",
            record.content.strip(),
            "",
            "---",
            "",
            "## Media",
            "",
        ]
        if not record.images and not record.videos:
            lines.append("No media captured.")
        for index, image in enumerate(record.images, start=1):
            target = image.local_path or image.url
            lines.append(f"- Image {index}: {target}")
        for index, video in enumerate(record.videos, start=1):
            lines.append(f"- Video {index}: {video.url}")

        if record.quoted_tweet:
            lines.extend(
                [
                    "",
                    "---",
                    "",
                    "## Quoted Tweet",
                    "",
                    f"URL: {record.quoted_tweet.get('url', '')}",
                    "",
                    record.quoted_tweet.get("text", ""),
                ]
            )

        lines.extend(["", "---", "", "## Top Replies", ""])
        if not record.replies:
            lines.append("No replies captured.")
        for reply in record.replies:
            heading = reply.author or reply.handle or reply.id or "Reply"
            lines.extend(
                [
                    f"### {heading}",
                    "",
                    f"Handle: {reply.handle or ''}",
                    f"Date: {reply.created_at or ''}",
                    f"URL: {reply.url or ''}",
                    "",
                    reply.text.strip(),
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _download_images(self, images: list[MediaItem], media_dir: Path) -> None:
        for index, image in enumerate(images, start=1):
            try:
                suffix = self._suffix_for_url(image.url) or ".jpg"
                digest = hashlib.sha256(image.url.encode("utf-8")).hexdigest()[:12]
                path = media_dir / f"image_{index:02d}_{digest}{suffix}"
                if not path.exists():
                    request = Request(image.url, headers={"User-Agent": "Mozilla/5.0"})
                    with urlopen(request, timeout=30) as response:
                        path.write_bytes(response.read())
                image.local_path = str(path.relative_to(media_dir.parent))
            except Exception as exc:  # noqa: BLE001 - archive should survive media failures.
                image.local_path = f"download_failed: {exc}"

    def _suffix_for_url(self, url: str) -> str:
        path = urlparse(url).path
        suffix = Path(path).suffix
        return suffix if suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ""
