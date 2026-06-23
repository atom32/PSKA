# PSKA Documentation Index

This is the current documentation map. It separates user-facing entry points,
developer/operations references, architecture/API references, and archived
historical planning notes.

## Quick Start

- [Root README](../README.md): shortest local command loop and component map.
- [Developer Quickstart](DEVELOPER_QUICKSTART.md): one-command dev stack, first-time setup, cold-start checks, and FastReAct integration.
- [Release, Init, and FastReAct Guide](RELEASE_INIT_FASTREACT_GUIDE.zh.md): Chinese operational guide for local release/init and real FastReAct linkage.

## Daily Use

- `./start.sh` starts the local backend supervisor and frontend workspace.
- `./scripts/pska --config .pska/config.json digest-now` syncs files/Twitter archives and processes one digest pass.
- `./scripts/pska --config .pska/config.json daily-status` shows deterministic readiness and backlog status.
- `./scripts/pska --config .pska/config.json review-list --status pending --summary` shows work waiting for human review.

The digest scheduler is incremental. The local daemon checks every 300 seconds
by default; it is not a fixed once-per-day cron.

Directory-level material packs can opt into source collection ingest with
`.pska-source.json`; see the [Operations Runbook](../core/docs/operations-runbook-zh.md#source-collection-marker).

## Architecture And API

- [Architecture](ARCHITECTURE.md): current system shape, source-centric flow, discovery invariants, and scheduler behavior.
- [API Reference](API_REFERENCE.md): HTTP endpoints used by the workspace, CLI, and integrations.
- [Feature Reality Check](FEATURE_REALITY_CHECK.md): what is shipped, partial, or design-only.
- [Telemetry Design](TELEMETRY.md): design-only telemetry notes.
- [Product Design](../core/docs/product-design-zh.md): Chinese product direction and user workflow.
- [Architecture Status](../core/docs/architecture-status-zh.md): Chinese module maturity map.
- [Vision](../core/docs/vision-zh.md): long-term Chinese vision.

## Operations

- [Operations Runbook](../core/docs/operations-runbook-zh.md): local daemon, database, status, jobs, and recovery commands.
- [Online Service Contract](../core/docs/service-contract-zh.md): HTTP service, auth/request context, jobs, candidates, connectors, and digest APIs.
- [FastReAct Boundary](../core/docs/fastreact-agentic-boundary-zh.md): PSKA/FastReAct responsibility split.
- [FastReAct Protocol](../core/docs/fastreact-protocol-zh.md): detailed protocol notes.
- [FastReAct Real Integration Manual](../core/docs/fastreact-pska-real-integration-manual-zh.md): real local integration workflow.

## Frontend

- [Frontend README](../frontend/README.md): User Workspace run command and surfaces.
- [Backend Feature Map](../frontend/BACKEND_FEATURES.md): frontend-to-backend capability map.

Current frontend status: User Workspace scaffold exists with Today,
Discoveries, Corpus, Graph, and review-oriented surfaces. Knowledge Sources and
file/folder management UI remain next-step product work.

## Twitter/X

- [Twitter/X Channel README](../channels/twitter-x/README.md)
- [Twitter/X Channel README zh](../channels/twitter-x/README.zh.md)
- [Archive Schema v2](../channels/twitter-x/docs/schema.md)

## Archive

Archived documents are historical reference, not the current plan of record:

- [MVP Status](archive/core/mvp-status.md)
- [MVP Status zh](archive/core/mvp-status-zh.md)
- [MVP User Scope zh](archive/core/mvp-user-scope-zh.md)
- [Roadmap/TODO zh](archive/core/roadmap-todo-zh.md)
- [Todo Implement System zh](archive/core/todo-implement-system-zh.md)
