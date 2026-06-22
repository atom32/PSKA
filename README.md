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

[→ PSKA Core README](core/README.md) | [→ Vision](core/docs/vision-zh.md) | [→ MVP Status](core/docs/mvp-status.md) | [→ Roadmap TODO](core/docs/roadmap-todo-zh.md)

## Quick Start

### Local Release And Initialization

For the full setup, startup, release-check, and FastReAct integration flow, see
[Release, Init, and FastReAct Guide](docs/RELEASE_INIT_FASTREACT_GUIDE.zh.md).

### Runtime Setup

PSKA standardizes on a local Python 3.12 virtual environment so the core,
Twitter/X channel, and BGE-M3 embedding stack stay portable:

```bash
brew install python@3.12
./scripts/bootstrap_pska_env
mkdir -p .pska
cp core/config.pska.example.json .pska/config.json
```

Edit `.pska/config.json`; this is the single local configuration file for the
database, workspace, HTTP service, LLM, FastReAct, embeddings, files roots, and
startup behavior. Do not configure PSKA startup through `PSKA_*` environment
variables.

Minimal shape:

```json
{
  "database": { "url": "postgresql:///pska" },
  "workspace": { "root": "~/PSKA_workspaces/default" },
  "startup": {
    "bootstrap": true,
    "backend": true,
    "frontend": { "enabled": true, "host": "127.0.0.1", "port": 5173 }
  },
  "service": { "host": "127.0.0.1", "port": 8765, "service_token": null },
  "llm": {
    "api_key_file": "~/api_key.txt",
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "timeout_seconds": 60
  },
  "fastreact": {
    "url": "http://127.0.0.1:8000",
    "service_token": null,
    "timeout_seconds": 30
  },
  "agentic_service": {
    "provider": "fastreact",
    "url": "http://127.0.0.1:8000",
    "service_token": null,
    "timeout_seconds": 30
  },
  "embedding": {
    "provider": "disabled",
    "model": "BAAI/bge-m3",
    "dimensions": 1024,
    "batch_size": 16
  },
  "ingest": { "chunk_size": 1200, "chunk_overlap": 0 },
  "files": {
    "roots": ["~/PSKA_workspaces/default/notes"],
    "ignore": ["*.tmp", "*.bak"],
    "max_bytes": 1000000,
    "owner_user_id": "user_primary",
    "space_id": "private_primary",
    "visibility": "private"
  }
}
```

Then start everything with:

```bash
./start.sh
```

`./start.sh` reads `.pska/config.json`, prepares the configured database and
knowledge sources when `startup.bootstrap` is true, starts the backend when
`startup.backend` is true, and starts the frontend when
`startup.frontend.enabled` is true.

### Twitter/X Archiving

```bash
cd channels/twitter-x
../../.pska/venvs/pska-py312/bin/python -m playwright install chromium

# Archive a tweet
archive save https://x.com/user/status/123456789
```

Or use the Chrome extension:

1. Open `chrome://extensions`, enable Developer mode
2. Load unpacked: `channels/twitter-x/extension/`
3. Navigate to a tweet and click the extension icon

### PSKA Core Ingestion

PSKA runtime data defaults to `~/PSKA_workspaces/default`. Keep user archives,
imports, run files, and logs out of the source repository; `workspaces/default/`
is ignored and should not be used as the active runtime workspace.

```bash
./scripts/pska --config .pska/config.json \
  import-twitter-zips \
  --input ~/PSKA_workspaces/default/twitter_archive
```

For a clean end-to-end local check:

```bash
./scripts/pska-cold-start-e2e --workspace-root ~/PSKA_workspaces/default --reset
```

### Search

```bash
./scripts/pska --config .pska/config.json \
  agentic-search --query "your question"
```

### User Workspace

```bash
./start.sh
```

This starts the local PSKA backend supervisor and the React/TypeScript
workspace frontend. Open <http://127.0.0.1:5173/>.

Developer docs:

- [Developer Quickstart](docs/DEVELOPER_QUICKSTART.md)
- [Architecture](docs/ARCHITECTURE.md)
- [API Reference](docs/API_REFERENCE.md)
- [Feature Reality Check](docs/FEATURE_REALITY_CHECK.md)
- [Telemetry Design](docs/TELEMETRY.md)

## Status

| Component | Status |
|-----------|--------|
| Twitter/X Channel | ✅ MVP Complete |
| PSKA Core | ✅ MVP Complete |
| Chrome Extension | ✅ v0.4.0 |
| LLM Extraction | ✅ Implemented |
| Agentic Search | ✅ Implemented |
| MCP Interface | ✅ Implemented |
| Production UI | 🟡 User Workspace scaffold in `frontend/` |
| Async Jobs | ✅ Durable MVP |

See [core/docs/mvp-status.md](core/docs/mvp-status.md) for detailed status and
[core/docs/roadmap-todo-zh.md](core/docs/roadmap-todo-zh.md) for the next-stage TODO list.

## License

MIT
