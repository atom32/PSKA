from __future__ import annotations

import json

import pska.archive
from pska.archive import ArchiveBuilder
from pska.models import ArchiveRecord, Comment, MediaItem


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self) -> bytes:
        return b"image-bytes"


def test_archive_builder_writes_core_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pska.archive, "urlopen", lambda request, timeout: FakeResponse())
    record = ArchiveRecord(
        id="123",
        url="https://x.com/alice/status/123",
        author="Alice",
        handle="@alice",
        content="Hello\nworld",
        created_at="2026-06-10T00:00:00.000Z",
        images=[MediaItem(url="https://example.invalid/image.jpg", kind="image")],
        videos=[MediaItem(url="https://video.example/clip.mp4", kind="video")],
        replies=[
            Comment(
                id="456",
                url="https://x.com/bob/status/456",
                author="Bob",
                handle="@bob",
                text="Nice post",
                raw_text="Bob\n@bob\nNice post",
            )
        ],
        raw_html="<html></html>",
        screenshot=b"png",
    )

    tweet_dir = ArchiveBuilder(tmp_path / "archive").write(record)

    assert (tweet_dir / "raw.html").read_text(encoding="utf-8") == "<html></html>"
    assert (tweet_dir / "screenshot.png").read_bytes() == b"png"
    assert (tweet_dir / "media").is_dir()
    assert list((tweet_dir / "media").glob("image_01_*.jpg"))
    markdown = (tweet_dir / "content.md").read_text(encoding="utf-8")
    assert "# Tweet" in markdown
    assert "Hello\nworld" in markdown
    assert "Nice post" in markdown
    comments = json.loads((tweet_dir / "comments.json").read_text(encoding="utf-8"))
    assert comments[0]["id"] == "456"
    assert comments[0]["author"]["handle"] == "@bob"
    assert comments[0]["content"]["text"] == "Nice post"
    assert comments[0]["source"] == "visible_dom"
    metadata = json.loads((tweet_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "pska.archive.v2"
    assert metadata["source"] == "twitter"
    assert metadata["record_type"] == "tweet"
    assert metadata["source_id"] == "123"
    assert metadata["canonical_url"] == "https://x.com/alice/status/123"
    assert metadata["author"]["handle"] == "@alice"
    assert metadata["author"]["profile_url"] == "https://x.com/alice"
    assert metadata["content"]["text"] == "Hello\nworld"
    assert metadata["capture"]["method"] == "python_cli"
    assert metadata["artifacts"]["metadata"] == "metadata.json"
    assert metadata["pska"]["owner_user_id"] == "user_primary"
    assert metadata["extraction"]["status"] == "ok"
    assert metadata["media"][0]["kind"] == "image"
    assert metadata["media"][0]["download_status"] == "ok"
    assert metadata["comments"][0]["id"] == "456"
    assert "raw_html" not in metadata
    payload = json.loads((tweet_dir / "pska_payload.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "pska.channel_ingest.v1"
    assert payload["source_id"] == "123"
    assert payload["raw_paths"]["metadata"] == "metadata.json"
