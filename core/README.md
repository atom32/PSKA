# PSKA Core

PSKA Core owns the source-centric knowledge model, database migrations, local
HTTP service, CLI, jobs, review/candidate boundary, search, local daemon, and
MCP tools.

Use the top-level documentation index for the current map:

- [Documentation Index](../docs/README.md)
- [中文文档索引](../docs/README.zh.md)

## Current Model

Runtime knowledge starts from first-class sources. Config roots are startup
defaults/seeds; runtime source and sync state live in the database. File sync
ingests configured folder sources, optional PDF/DOCX extraction, and the
workspace Twitter/X archive inbox, using content hashes for incremental work.

The digest path is incremental. `digest-scheduler` checks for new or changed
sources on an interval, while `digest-now` runs sync and one manual digest pass.
FastReAct digest failures or no-candidate runs are surfaced through diagnostics
and fallback review items.

## Useful Commands

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
./start.sh
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

## Key References

- [API Reference](../docs/API_REFERENCE.md)
- [Architecture](../docs/ARCHITECTURE.md)
- [Operations Runbook](docs/operations-runbook-zh.md)
- [Online Service Contract](docs/service-contract-zh.md)
- [Product Design](docs/product-design-zh.md)
- [Architecture Status](docs/architecture-status-zh.md)
