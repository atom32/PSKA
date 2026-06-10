# PSKA Twitter/X Channel

Twitter/X acquisition channel for PSKA.

Archives use the PSKA archive v2 metadata schema documented in `docs/schema.md`.

## Install

From the repository root:

```bash
./scripts/bootstrap_pska_env
.pska/venvs/pska-py312/bin/python -m playwright install chromium
```

## Usage

```bash
archive login twitter
archive save https://x.com/user/status/123456789
archive batch urls.txt
```

Equivalent module form:

```bash
PYTHONPATH=channels/twitter-x/src .pska/venvs/pska-py312/bin/python -m pska.cli save https://x.com/user/status/123456789
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
4. Select this folder from the repository root:

```text
channels/twitter-x/extension/
```

If you are already in `channels/twitter-x`, select `extension/`.

Usage:

1. Open a Tweet/X status page in Chrome.
2. Click the PSKA Archive extension.
3. Click Archive current Tweet, or paste multiple URLs into Batch URLs.

Chrome downloads one ZIP per Tweet under:

```text
Downloads/twitter_archive/<tweet_id>.zip
```

The ZIP contains:

```text
<tweet_id>/
  raw.html
  screenshot.png
  content.md
  comments.json
  metadata.json
  media/
```

Batch mode also writes `Downloads/twitter_archive/batch_report.zip`.

The extension captures a visible tab screenshot after returning to the top of the
page. It does not bypass Twitter/X media restrictions; videos are saved as links
in Markdown/JSON.
