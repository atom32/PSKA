# PSKA Archive Schema v1

Each archive writes `metadata.json` using `schema_version: "pska.archive.v1"`.
Convenience files such as `content.md`, `comments.json`, `raw.html`,
`screenshot.png`, and `media/` may also be present.

```json
{
  "schema_version": "pska.archive.v1",
  "source": "twitter",
  "record_type": "tweet",
  "id": "123",
  "url": "https://x.com/user/status/123",
  "author": {
    "name": "User",
    "handle": "@user"
  },
  "content": {
    "text": "Post text"
  },
  "created_at": "2026-06-10T00:00:00.000Z",
  "captured_at": "2026-06-10T00:00:10.000Z",
  "media": [
    {
      "kind": "image",
      "url": "https://pbs.twimg.com/media/example?format=jpg&name=orig",
      "local_path": "media/image_01.jpg",
      "alt_text": null,
      "content_type": "image/jpeg"
    }
  ],
  "comments": [],
  "metrics": {},
  "extra": {}
}
```

`record_type` is currently `tweet` or `x_article`.
