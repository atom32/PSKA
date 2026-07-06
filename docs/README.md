# PSKA Documentation Index

This is the current documentation map. It separates user-facing entry points,
developer/operations references, architecture/API references, and archived
historical planning notes.

## Quick Start

- [Root README](../README.md): multi-tenant developer entrypoint, service map,
  identity headers, source/digest/Ask workflow, and verification commands.
- [Developer Quickstart](DEVELOPER_QUICKSTART.md): one-command dev stack, first-time setup, cold-start checks, and FastReAct integration.
- [Release, Init, and FastReAct Guide](RELEASE_INIT_FASTREACT_GUIDE.zh.md): Chinese operational guide for local release/init and real FastReAct linkage.
- [v0.1.0 Phase 1 Release Notes](RELEASE_V0_1_PHASE1.zh.md): Chinese release baseline notes for the Evidence QA Engine, browser validation, tags, and deferred work.
- [Phase 1 Multi-KB RAG Release Notes](RELEASE_PHASE1_MULTI_KB_RAG.zh.md): Chinese release candidate notes, verification commands, and scope boundaries for the multi-knowledge-base RAG foundation.
- [Configuration Contract](CONFIGURATION_CONTRACT.zh.md): Chinese reference for PSKA, FastReAct, and AuthNode config locations, startup entry points, and compatibility fields.

## Daily Use

- `./start.sh` starts the local backend supervisor and frontend workspace.
- `./scripts/pska --config .pska/config.json digest-now` syncs files/Twitter archives and processes one digest pass.
- `./scripts/pska --config .pska/config.json daily-status` shows deterministic readiness and backlog status.
- `./scripts/pska --config .pska/config.json review-list --status pending --summary` shows work waiting for human review.

Digest is manual by default. `./start.sh` does not start the periodic digest
scheduler; use `digest-now` or `digest-schedule` explicitly when you want to
spend FastReAct/LLM capacity on digest work.

Directory-level material packs can opt into source collection ingest with
`.pska-source.json`; see the [Operations Runbook](../core/docs/operations-runbook-zh.md#source-collection-marker).

## Architecture And API

- [Architecture](ARCHITECTURE.md): current system shape, source-centric flow, discovery invariants, and scheduler behavior.
- [Phases](PHASES.md): one-page Phase 0/1/2/3 map and Phase 1 baseline.
- [Evidence-driven QA Engine](PSKA_EVIDENCE_QA_ENGINE.zh.md): Chinese architecture note for the Retrieval → Evidence → Citation → Answer pipeline, audit schema, timeline, and regression strategy.
- [Architecture Decision Records](adr/README.md): accepted engineering decisions that constrain future PSKA changes.
- [RFC 0002: Multi-evidence Composition](rfcs/0002-multi-evidence-composition.md): Phase 2 design for Evidence Sets, comparison/trend QA, and composition boundaries.
- [Embedding Job Control](EMBEDDING_JOB_CONTROL.zh.md): Chinese operational note for scoped embedding backfill jobs, KB coverage, and Phase 2 controls.
- [Digest Job Control](DIGEST_JOB_CONTROL.zh.md): Chinese operational note for digest scheduler quota, backlog limits, and FastReAct worker boundaries.
- [API Reference](API_REFERENCE.md): HTTP endpoints used by the workspace, CLI, and integrations.
- [Feature Reality Check](FEATURE_REALITY_CHECK.md): what is shipped, partial, or design-only.
- [Phase 1 Multi-KB RAG Milestone](MILESTONE_PHASE1_MULTI_KB_RAG.zh.md): Chinese milestone and acceptance evidence for one-account-many-knowledge-bases.
- [Reader/Ask Product Slice Milestone](MILESTONE_READER_ASK_PRODUCT_SLICE.zh.md): Chinese milestone for citation inspection, source-focused follow-up, and ReaderPane work; this is a reusable product slice, not the overall Phase 2 roadmap.
- [WeKnora Core Coverage Acceptance](WEKNORA_COVERAGE.zh.md): Chinese
  multi-tenant coverage checklist, E2E script, and competitor comparison bar.
- [Telemetry Design](TELEMETRY.md): design-only telemetry notes.
- [Product Design](../core/docs/product-design-zh.md): Chinese product direction and user workflow.
- [Architecture Status](../core/docs/architecture-status-zh.md): Chinese module maturity map.
- [Vision](../core/docs/vision-zh.md): long-term Chinese vision.

## Operations

- [Operations Runbook](../core/docs/operations-runbook-zh.md): local daemon, database, status, jobs, and recovery commands.
- [Online Service Contract](../core/docs/service-contract-zh.md): HTTP service, auth/request context, jobs, candidates, connectors, and digest APIs.
- [Configuration Contract](CONFIGURATION_CONTRACT.zh.md): PSKA/FastReAct/AuthNode local config path and redundancy notes.
- [Enterprise Auth Gateway](ENTERPRISE_AUTH_GATEWAY.zh.md): AuthNode + PSKA Gateway browser login, session, and reverse-proxy flow.
- [FastReAct Boundary](../core/docs/fastreact-agentic-boundary-zh.md): PSKA/FastReAct responsibility split.
- [FastReAct Protocol](../core/docs/fastreact-protocol-zh.md): detailed protocol notes.
- [FastReAct Real Integration Manual](../core/docs/fastreact-pska-real-integration-manual-zh.md): real local integration workflow.

## Frontend

- [Frontend README](../frontend/README.md): User Workspace run command and surfaces.
- [Backend Feature Map](../frontend/BACKEND_FEATURES.md): frontend-to-backend capability map.

Current frontend status: User Workspace includes Today, Discoveries, the
document library, multi-turn Ask, Graph, review-oriented flows, prompt
profiles, and Writing/Evidence Brief surfaces. User-facing ingest supports
upload, pasted text, URL, and RSS. Folder sources remain available for
admin/dev migration flows. Internally these flows still use KnowledgeSource,
source item, document, chunk, digest, and review models.

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
