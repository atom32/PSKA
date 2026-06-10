# PSKA Archive Schema v2

Each archive writes `metadata.json` using `schema_version: "pska.archive.v2"`.
Convenience files such as `content.md`, `comments.json`, `raw.html`,
`screenshot.png`, and `media/` are artifacts referenced by `metadata.json`.
Archives also write `pska_payload.json`, a derived convenience file for PSKA
Core ingest. `metadata.json` remains the authoritative archive record.

```json
{
  "schema_version": "pska.archive.v2",
  "source": "twitter",
  "record_type": "tweet",
  "source_id": "123",
  "url": "https://x.com/user/status/123",
  "canonical_url": "https://x.com/user/status/123",
  "author": {
    "name": "User",
    "handle": "@user",
    "profile_url": "https://x.com/user"
  },
  "content": {
    "text": "Post text",
    "raw_text": "Visible DOM text",
    "language": null
  },
  "created_at": "2026-06-10T00:00:00.000Z",
  "captured_at": "2026-06-10T00:00:10.000Z",
  "capture": {
    "method": "chrome_extension",
    "extension_version": "0.4.0",
    "page_url": "https://x.com/user/status/123",
    "visible_comment_limit": 50
  },
  "media": [
    {
      "kind": "image",
      "url": "https://pbs.twimg.com/media/example?format=jpg&name=orig",
      "local_path": "media/image_01.jpg",
      "alt_text": null,
      "content_type": "image/jpeg",
      "download_status": "ok"
    }
  ],
  "comments": [],
  "quoted_items": [],
  "metrics": {},
  "artifacts": {
    "metadata": "metadata.json",
    "markdown": "content.md",
    "comments": "comments.json",
    "raw_html": "raw.html",
    "screenshot": "screenshot.png",
    "media_dir": "media/"
  },
  "pska": {
    "owner_user_id": "user_primary",
    "space_id": "private_primary",
    "visibility": "private",
    "visible_team_ids": []
  },
  "extraction": {
    "status": "ok",
    "warnings": [],
    "source": "visible_dom"
  },
  "extra": {}
}
```

`record_type` is currently `tweet` or `x_article`.
