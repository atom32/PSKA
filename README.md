# PSKA

Personal Social Knowledge Archive is a private-first local knowledge workspace.
It ingests local files and Twitter/X archives, stores source material in
PostgreSQL, indexes documents and chunks, exposes search/review/digest through
CLI and HTTP, and delegates agentic digestion to FastReAct when configured.

## Start Here

For the complete documentation map, use:

- [Documentation Index](docs/README.md)
- [中文文档索引](docs/README.zh.md)

The shortest local loop is:

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
./start.sh
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json mvp-status --summary
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

`db-reset` is destructive and recreates the named local database. Use it only
when you intentionally want a fresh local PSKA database.

`./start.sh` reads `.pska/config.json`, prepares configured knowledge sources
when bootstrap is enabled, starts the backend supervisor, and starts the
frontend workspace when enabled.

`digest-now` runs file sync first. The sync path covers folder sources,
PDF/DOCX text extraction when optional extractors are installed, the workspace
Twitter/X archive inbox, and content-hash based incremental handling. It then
schedules and processes one digest pass.

## Daily Operation

The background digest scheduler is incremental, not a fixed daily cron. When
`./start.sh` launches the local daemon, `pska-digest-scheduler` checks for new
or changed sources every 300 seconds by default. Sources already covered by a
current digest job are skipped unless they changed or a manual command uses
`--force`.

Useful daily commands:

```bash
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json daily-briefing --owner-user-id user_primary --limit 5
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

If FastReAct processes a digest job without writing candidates, PSKA surfaces
that as diagnostics and a fallback review item instead of silently showing
empty review results.

## Main Components

- `core/`: source-centric knowledge model, ingestion, jobs, search, review,
  HTTP API, local daemon, and MCP tools.
- `frontend/`: User Workspace scaffold with Today, Discoveries, Corpus, Graph,
  and review-oriented surfaces. Knowledge Sources/file management UI remains a
  next step.
- `channels/twitter-x/`: Twitter/X archive collector and archive schema.

## Runtime Data

Runtime/user data defaults to `~/PSKA_workspaces/default`. Keep imports, logs,
run files, and active workspace data out of the source repository.

`.pska/config.json` is local machine configuration and should not be committed.
Treat config as startup/default seed; runtime source and sync state live in the
database.

## License

MIT
