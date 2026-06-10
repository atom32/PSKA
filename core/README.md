# PSKA Core

PSKA Core owns the private-first knowledge model, ACL rules, memory objects,
hypergraph primitives, and retrieval interfaces for the PSKA workspace.

Current MVP status is tracked in [`docs/mvp-status.md`](docs/mvp-status.md).
The next-stage implementation TODO is tracked in
[`docs/roadmap-todo-zh.md`](docs/roadmap-todo-zh.md).

The core is intentionally separate from channel collectors. Channel projects
such as `channels/twitter-x` collect and normalize raw source material; PSKA
Core registers, indexes, searches, and governs access to that material.

## Storage Direction

The production target is PostgreSQL with `pgvector`. The migration in
`src/pska_core/migrations/001_init.sql` defines the v1 schema. The Python
services include an in-memory implementation used by tests and early agent
integration.

## Privacy Rules

- Users and teams are anonymous identifiers.
- Real names, kinship labels, aliases, secrets, and local paths must stay out of
  committed config.
- Knowledge is private by default.
- Team visibility is explicit through `visible_team_ids`.
- Agent-generated memory belongs to the represented user, never to the
  `agent_service` identity.

## Local Smoke

```bash
./.pska/venvs/pska-py312/bin/python core/scripts/e2e_smoke.py
```

The smoke script resets `pska_smoke`, imports `~/Downloads/twitter_archive/*.zip`,
runs LLM extraction, CLI search, agentic search, HTTP API checks, direct MCP
checks, and Fastreact MCP loading.

## Twitter Zip Import

```bash
./scripts/pska db-reset --name pska_smoke
./scripts/pska --database-url postgresql:///pska_smoke \
  import-twitter-zips \
  --input ~/Downloads/twitter_archive \
  --archive-root archive/imports
```

Canonical new archives should use `pska.archive.v2`. Legacy Twitter zip metadata
is accepted only as a compatibility path.

## BGE-M3 Embeddings

PSKA's P0 embedding path uses local BGE-M3 through `FlagEmbedding` and stores
1024-dimensional vectors in PostgreSQL `pgvector`.

Use the repository wrapper so the command runs inside the local Python 3.12
environment:

```bash
brew install python@3.12
./scripts/bootstrap_pska_env
```

Backfill imported chunks:

```bash
./scripts/pska --database-url postgresql:///pska_smoke \
  embed-backfill \
  --embedding-provider bge-m3
```

Run hybrid search with vector retrieval enabled:

```bash
./scripts/pska --database-url postgresql:///pska_smoke \
  search \
  --query "agentic coding automation" \
  --embedding-provider bge-m3
```

When `PSKA_EMBEDDING_PROVIDER=bge-m3` is set, CLI/API/MCP retrieval uses the
same BGE-M3 provider. If the provider cannot be loaded, the command fails
explicitly rather than silently falling back to lexical search.

## Fastreact MCP Boundary

Fastreact can load PSKA as a read-only stdio MCP server without importing PSKA
internals:

```bash
export PSKA_DATABASE_URL=postgresql:///pska_smoke
export FASTRACT_MCP_SERVERS='[{"name":"pska","command":"./scripts/pska","args":["mcp-server"],"isolation":"shared"}]'
```

API keys should be injected into the Fastreact process environment only. Do not
write them into repository config.
