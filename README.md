# PSKA

Personal Social Knowledge Archive — A private-first knowledge management system with LLM-assisted extraction, hypergraph memory, and ACL-governed retrieval.

## Overview

PSKA is an end-to-end personal knowledge archive that:

- **Collects** content from social platforms (Twitter/X initially)
- **Normalizes** content into a canonical schema (`pska.archive.v2`)
- **Stores** artifacts in PostgreSQL with privacy-first ACLs
- **Extracts** entities and relationships via LLM
- **Builds** a hypergraph memory model
- **Retrieves** with agentic LLM-planned search
- **Exposes** results via CLI, HTTP API, and MCP

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        PSKA System                           │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  channels/               core/                               │
│  ┌────────────────────┐       ┌──────────────────────┐      │
│  │  twitter-x/        │ ──>   │  Knowledge Store      │      │
│  │    Chrome ext      │  ZIP  │  PostgreSQL+pgvector  │      │
│  │    Python CLI      │       │  ACL / Privacy        │      │
│  │    schema v2       │       │  LLM Extraction        │      │
│  └────────────────────┘       │  Hypergraph Memory    │      │
│                               │  Retrieval Service    │      │
│                               │  MCP Server           │      │
│                               └──────────────────────┘      │
│                                      │                       │
│                                      v                       │
│                               ┌──────────────┐               │
│                               │  Fastreact   │               │
│                               │  (Frontend)   │               │
│                               └──────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

## Components

### channels/twitter-x/

Twitter/X acquisition channel with Chrome extension and Python CLI.

- **Chrome Extension** (`extension/`): Archive tweets from your logged-in browser
- **Python CLI** (`src/pska/`): Command-line collection tool
- **Schema** (`docs/schema.md`): PSKA v1 metadata specification

[→ Twitter-X Channel README](channels/twitter-x/README.md)

### core/

PSKA Core implements the knowledge model, storage, and services.

- **Data Model**: Users, teams, spaces, sources, documents, chunks, entities, hyperedges
- **Privacy**: Anonymous IDs, private-first visibility, team ACLs
- **LLM Integration**: Entity/hyperedge extraction, agentic search
- **APIs**: CLI, HTTP, stdio MCP

[→ PSKA Core README](core/README.md) | [→ MVP Status](core/docs/mvp-status.md) | [→ Roadmap TODO](core/docs/roadmap-todo-zh.md)

## Quick Start

### Twitter/X Archiving

```bash
cd channels/twitter-x
python3 -m pip install -e .
python3 -m playwright install chromium

# Archive a tweet
archive save https://x.com/user/status/123456789
```

Or use the Chrome extension:

1. Open `chrome://extensions`, enable Developer mode
2. Load unpacked: `channels/twitter-x/extension/`
3. Navigate to a tweet and click the extension icon

### PSKA Core Ingestion

```bash
cd core
PYTHONPATH=src python3 -m pska_core.cli db-reset --name pska_smoke
PYTHONPATH=src python3 -m pska_core.cli \
  --database-url postgresql:///pska_smoke \
  import-twitter-zips \
  --input ~/Downloads/twitter_archive
```

### Search

```bash
PYTHONPATH=src python3 -m pska_core.cli \
  --database-url postgresql:///pska_smoke \
  agentic-search --query "your question"
```

## Status

| Component | Status |
|-----------|--------|
| Twitter/X Channel | ✅ MVP Complete |
| PSKA Core | ✅ MVP Complete |
| Chrome Extension | ✅ v0.4.0 |
| LLM Extraction | ✅ Implemented |
| Agentic Search | ✅ Implemented |
| MCP Interface | ✅ Implemented |
| Production UI | ❌ Not Started |
| Async Jobs | ❌ Not Started |

See [core/docs/mvp-status.md](core/docs/mvp-status.md) for detailed status and
[core/docs/roadmap-todo-zh.md](core/docs/roadmap-todo-zh.md) for the next-stage TODO list.

## License

MIT
