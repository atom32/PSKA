# PSKA Twitter Archive

Personal Social Knowledge Archive V1 for Twitter/X.

## Install

```bash
python3 -m pip install -e .
python3 -m playwright install chromium
```

If `uv` is available:

```bash
uv sync
uv run playwright install chromium
```

## Usage

```bash
archive login twitter
archive save https://x.com/user/status/123456789
archive batch urls.txt
```

Equivalent module form:

```bash
python3 -m pska.cli save https://x.com/user/status/123456789
```

Archives are written to:

```text
archive/twitter/<tweet_id>/
  raw.html
  screenshot.png
  content.md
  comments.json
  metadata.json
  media/
```

Configuration is created at `.pska/config.toml` on first run.

## Chrome Extension

If Twitter/X limits Playwright login, use the unpacked Chrome extension in `extension/`.
It runs inside your already logged-in Chrome session.

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked.
4. Select this folder:

```text
extension/
```

Usage:

1. Open a Tweet/X status page in Chrome.
2. Click the PSKA Archive extension.
3. Click Archive current Tweet, or paste multiple URLs into Batch URLs.

Chrome downloads files under:

```text
Downloads/twitter_archive/<tweet_id>/
  raw.html
  screenshot.png
  content.md
  comments.json
  metadata.json
  media/
```

Batch mode also writes `Downloads/twitter_archive/batch_report.json`.

The extension captures a visible tab screenshot after returning to the top of the
page. It does not bypass Twitter/X media restrictions; videos are saved as links
in Markdown/JSON.
