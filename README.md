# PSKA

Personal Social Knowledge Archive is a private-first, multi-tenant local
knowledge workspace. It ingests tenant/user scoped sources, turns them into
documents, chunks, digest notes, review candidates, memories, graph
relationships, citations, and writing evidence. PSKA is designed to be more
than a RAG chat surface: the durable product loop is
`Digest -> Candidates -> Review -> Discovery -> Graph/Memory -> Writing`.

For the Chinese version, see [README.zh.md](README.zh.md).

## Start Here

- [Documentation Index](docs/README.md)
- [中文文档索引](docs/README.zh.md)
- [Developer Quickstart](docs/DEVELOPER_QUICKSTART.md)
- [Enterprise Auth Gateway](docs/ENTERPRISE_AUTH_GATEWAY.zh.md)
- [Multi-tenant Workspace E2E](docs/MULTITENANT_WORKSPACE_E2E.zh.md)
- [WeKnora Core Coverage Acceptance](docs/WEKNORA_COVERAGE.zh.md)

## Tenant Build At A Glance

- Branch policy: multi-tenant/AuthNode/FastReAct integration work belongs on
  the `tenant` branch. Do not promote it to `master` unless the owner asks.
- Identity model: every request is scoped by `tenant_id`, `user_id`, and
  optional `represented_user_id`.
- Browser auth: normal multi-tenant browser access goes through PSKA Gateway
  and AuthNode. The browser should only hold a HttpOnly gateway session cookie.
- Agentic work: Deep Ask and digest generation are delegated to FastReAct when
  configured. PSKA owns readiness checks, MCP boundaries, citations, review
  gating, and tenant visibility.
- Core coverage: the user-facing document library, processing spans, chunk
  preview, Digest, Review, multi-turn Ask with evidence, Evidence
  Briefs/Writing, prompt profiles, and readiness diagnostics are first-class
  tenant-version surfaces. Internally this still maps to KnowledgeSource,
  source item, document, chunk, and review/digest models.

## Local Services

| Service | Default URL | Started from | Purpose |
| --- | --- | --- | --- |
| AuthNode | `http://127.0.0.1:8788` | AuthNode repo `./start.sh` | Login, tenant/user claims, local IAM or OIDC |
| PSKA API | `http://127.0.0.1:8765` | this repo `./start.sh` | Knowledge, source sync, review, Ask, MCP |
| PSKA Gateway/UI | `http://127.0.0.1:5173` | this repo `./start.sh` | Browser entrypoint and frontend |
| FastReAct | `http://127.0.0.1:18741` | FastReAct repo startup | Agentic Ask/digest execution |

PSKA does not start AuthNode or FastReAct for you. Start those projects from
their own repositories, then start PSKA from this repository with `./start.sh`.
For PSKA verification, use the integrated `./start.sh` path unless you are
explicitly doing isolated debugging.

Command examples use path variables instead of machine-specific checkout paths:

```bash
export PSKA_REPO="$(pwd)"
export AUTHNODE_REPO="/path/to/AuthNode"
export FASTREACT_REPO="/path/to/FastReAct"
export FASTREACT_NANO_REPO="$FASTREACT_REPO/fastreact-nano"
```

## First-Time Setup

```bash
brew install python@3.12
./scripts/bootstrap_pska_env
mkdir -p .pska
cp core/config.pska.example.json .pska/config.json
./scripts/pska --config .pska/config.json db-check
./scripts/pska --config .pska/config.json db-create --name pska
./scripts/pska --config .pska/config.json db-init
cd frontend
npm install
```

`.pska/config.json` is local machine configuration and must not be committed.
It may contain local paths, model keys, service tokens, and FastReAct tokens.

For login-protected multi-tenant browser testing, set the frontend mode to
`gateway`:

```json
"startup": {
  "frontend": {
    "enabled": true,
    "mode": "gateway",
    "host": "0.0.0.0",
    "port": 5173,
    "public_url": "http://127.0.0.1:5173"
  }
}
```

`host` is the bind address; `public_url` is the browser-facing PSKA Gateway URL
used for AuthNode callbacks. Set it to the LAN/ingress URL when testing off the
local machine.

Then start AuthNode and PSKA:

```bash
cd "$AUTHNODE_REPO"
./start.sh

cd "$PSKA_REPO"
export AUTHNODE_URL=http://127.0.0.1:8788
export PSKA_GATEWAY_SESSION_SECRET='<random-long-secret>'
./start.sh
```

Open:

```text
http://127.0.0.1:5173/
```

Use `startup.frontend.mode: "vite"` only when you explicitly want hot reload
and you understand that local Vite development uses frontend session storage
for its lightweight identity context.

## Daily Developer Loop

Start dependencies:

```bash
cd "$AUTHNODE_REPO"
./start.sh

cd "$FASTREACT_REPO"
./start.sh

cd "$PSKA_REPO"
./start.sh
```

Check the stack:

```bash
curl -s http://127.0.0.1:8788/health
curl -s http://127.0.0.1:8765/health
curl -s http://127.0.0.1:18741/health
./scripts/pska --config .pska/config.json service-check
```

If `.pska/config.json` sets `service.service_token`, direct API calls need
either `Authorization: Bearer <token>` or `X-PSKA-Service-Token: <token>`.

## Tenant Data Layout

Runtime data defaults to:

```text
~/PSKA_workspaces
```

The tenant build keeps system files and user content separate:

```text
~/PSKA_workspaces/_system/run
~/PSKA_workspaces/_system/logs
~/PSKA_workspaces/tenants/<tenant_id>/users/<user_id>/sources
```

`./start.sh` prepares the system directories but does not silently ingest user
content. Add tenant/user sources explicitly. The CLI currently has a folder
shortcut:

```bash
mkdir -p "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/sources"

./scripts/pska --config .pska/config.json knowledge-source add-folder \
  --tenant-id tenant_default \
  --owner-user-id user_primary \
  --space-id private_primary \
  --path "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/sources"

./scripts/pska --config .pska/config.json files-sync \
  --tenant-id tenant_default \
  --owner-user-id user_primary \
  --root "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/sources"
```

The Workspace UI and HTTP APIs support folder, RSS/Atom, and URL sources with
preview, sync, cleanup, retry, processing spans, and chunk preview.

## Digest, Review, Ask, Writing

Digest is the PSKA differentiator. A digest pass should produce source-backed
artifacts such as `digest_note`, `knowledge_claim`, `review_item`,
`memory_candidate`, and `relationship_candidate`. Durable knowledge writes must
carry `source_refs`; low-confidence or high-impact changes should go through
Review before becoming long-term memory or graph state.

Useful commands:

```bash
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json daily-briefing --owner-user-id user_primary --limit 5
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

Ask PSKA supports direct and FastReAct-backed flows. Responses should expose
citations, source/chunk previews, progress, evidence checks, and a clear
no-answer reason when PSKA cannot answer with visible evidence.

Evidence Briefs are the PSKA-style Wiki path: digest notes, Ask results, and
reviewed claims can become writing board drafts with citations, source refs,
lineage, and review status. PSKA does not auto-publish unreviewed wiki pages.

## API Cheatsheet

For API/CLI tests inside a trusted local boundary, send identity explicitly:

```bash
TOKEN='<service.service_token>'
curl -s http://127.0.0.1:8765/workspace/readiness \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-PSKA-Tenant-Id: tenant_default" \
  -H "X-PSKA-User-Id: user_primary" \
  -H "X-PSKA-Represented-User-Id: user_primary" \
  -H "X-PSKA-Subject: pska:user_primary"
```

Omit `Authorization` when local service auth is disabled.

Common workspace endpoints:

- `GET /workspace/readiness`
- `GET /console/sources/data`
- `GET /workspace/sources/adapters`
- `POST /workspace/sources/preview`
- `POST /workspace/sources`
- `POST /workspace/sources/sync`
- `POST /workspace/chunking/preview`
- `GET /workspace/digest/data`
- `POST /workspace/digest/run`
- `POST /workspace/ask`
- `POST /workspace/ask/stream`
- `POST /workspace/evidence-briefs`

For browser/SaaS use, do not expose service tokens or raw tenant headers to
users. Put the frontend behind PSKA Gateway/AuthNode, JWT mode, or a trusted
ingress that injects verified identity.

## Verification

Recommended local checks:

```bash
./start.sh
./scripts/pska --config .pska/config.json service-check
PYTHONPATH=core/src python -m pytest core/tests/test_fastreact_integration.py
PYTHONPATH=core/src python -m pytest core/tests
cd frontend
npm run build
```

Full multi-tenant Writing smoke:

```bash
./scripts/pska-writing-workspace-e2e --config ".pska/config.json"
```

WeKnora core coverage acceptance:

```bash
./scripts/pska-weknora-coverage-e2e --config ".pska/config.json"
```

## Engineering Constraints

- Keep PSKA domain-agnostic. Do not improve answer quality with sample-corpus,
  sample-company, or question-specific shortcuts.
- Use generic retrieval, digest, review, citation, and writing mechanisms with
  cross-domain tests.
- Keep imports, run files, logs, active workspace data, and local credentials
  out of the source repository.

## Main Components

- `core/`: tenant-aware knowledge model, source adapters, ingestion, jobs,
  search, digest, review, Ask, Evidence Briefs, HTTP API, local daemon, and MCP
  tools.
- `frontend/`: User Workspace with Today, Discoveries, document library,
  multi-turn Ask, Graph, Review-oriented flows, prompt profiles, and
  Writing/Evidence Brief surfaces.
- `channels/twitter-x/`: Twitter/X archive collector and archive schema.

## License

MIT
