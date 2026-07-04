# PSKA Feature Reality Check

This document separates shipped behavior from prototype presentation. Use it
when deciding whether a workspace surface is ready for real use, needs backend
connection, or is design-only.

Status legend:

- `Real`: backed by persistent data and working API behavior.
- `Partial`: some real data or API support exists, but the workflow is not end
  to end.
- `Mock`: visible in the UI, but powered by local sample data or static state.
- `Planned`: product/API concept exists, but no usable surface yet.

## Current Snapshot

PSKA's backend is source-centric. Runtime source/sync state lives in the
database; config only seeds default roots. `files-sync` handles active folder
sources, PDF/DOCX/XLSX text extraction, optional legacy XLS extraction, manifest reconciliation, and the
workspace Twitter/X archive inbox. `digest-now` runs sync before scheduling and
processing one digest pass.

The frontend is a User Workspace. It has real Today/Discoveries, Knowledge
Sources, Corpus/Search-backed surfaces, Ask evidence views,
Writing/Evidence Brief flows, and graph/review-adjacent panels. It is not a
full document editor, general canvas persistence layer, or enterprise connector
marketplace yet.

## Product Surface Matrix

| Feature | Backend | Frontend | Reality | Notes |
| --- | --- | --- | --- | --- |
| One-command local startup | Real | Real | Real | `./start.sh` starts the backend supervisor and Vite frontend. |
| Folder source sync | Real | Real | Real | Backend and Workspace UI support folder preview/create/sync plus sync reports. |
| RSS/Atom source sync | Real | Real | Real | SourceAdapter v1 supports preview/create/sync for RSS/Atom feeds. |
| URL page/sitemap sync | Real | Real | Real | SourceAdapter v1 supports preview/create/sync for URL pages and sitemaps. |
| Processing transparency | Real | Real | Real | Source cards expose sync runs, processing spans, diagnostics, cleanup, retry, and chunk preview. |
| Twitter/X archive import | Real | Partial | Partial | `files-sync` and `digest-now` import the workspace archive inbox by content hash. |
| Today aggregation | Real | Real | Partial | Uses real `/workspace/today/data`; empty real sections should render empty states. |
| Discovery feed | Real | Real | Partial | Persistent discoveries and score filtering exist; quality still depends on producers and corpus. |
| Needs Review | Real | Real | Partial | Review APIs and fallback digest review exist; full Review workspace is still evolving. |
| Review approve/reject/apply | Real | Partial | Partial | Backend supports review actions; Review Center can approve/reject/apply, snooze/restore pending candidates, compare Review source refs in a citation inspector/ReaderPane, compare multiple selected candidates side-by-side, show queue/decision analytics across review types, show type-specific remediation blockers/actions, show decision history across review states, show post-apply lineage with target IDs, retained evidence counts, and Memory/Profile target previews, bulk handle visible pending/approved/snoozed candidates, persist application targets, and jump applied relationship candidates into Graph inspection. Broader automated remediation executors are still incomplete. |
| Ask PSKA with evidence | Real | Real | Partial | Quick and FastReAct-backed Ask expose citations, source refs, progress, evidence preview, and no-answer diagnostics; answer quality still depends on retrieval/model readiness. |
| PSKA Brain search | Real | Real | Partial | Uses workspace search and corpus context; recall quality depends on indexing/embeddings. |
| Corpus browser | Real | Partial | Partial | Backend corpus endpoint exists; frontend uses it in selected panels. |
| Graph browser | Real | Partial | Partial | Graph data exists; visual exploration supports focused subgraphs from Review and citation-backed selected-node Writing drafts, but broader graph ergonomics are still early. |
| Evidence Brief / Writing draft | Real | Real | Partial | Digest/review artifacts, supported Ask runs, Graph Ask citations, and selected Graph nodes can generate citation-backed Writing board drafts; Writing now has an Evidence Brief Library for list/detail/re-digest/expire/rollback lifecycle work plus draft/published Wiki promotion status, publish review gates, default published Wiki navigation, scoped Wiki search, access hints, durable Wiki taxonomy, taxonomy facets/filters, page-level Wiki content edits backed by Writing nodes, cross-page related Wiki links, and a dedicated published page preview. |
| Document editor | Planned | Mock | Mock | Tiptap editor exists, but document persistence is not wired as a source editor. |
| Canvas | Planned | Mock | Mock | React Flow canvas is local/sample state. |
| Project/tag navigation | Planned | Mock | Mock | Sidebar labels exist without a durable project/tag model. |
| Telemetry | Planned | Planned | Planned | `docs/TELEMETRY.md` is design-only. |

## Digest Reality

The digest scheduler is incremental, not a daily cron. The local daemon checks
for new or changed sources every 300 seconds by default. Manual `digest-now`
runs sync first and then processes one digest pass.

If FastReAct processes a digest job without calling `pska_write_candidates`,
PSKA reports diagnostics and creates a fallback review item for the scheduled
sources. This prevents the product from appearing to succeed while showing an
empty review queue.

## Highest Priority Product Gaps

1. Extend Evidence Wiki beyond gated published pages into richer standalone
   Wiki-page workflows, stronger taxonomy governance, and fuller page lifecycle
   controls.
2. Expand SourceAdapter coverage beyond folder/RSS/URL when enterprise
   connectors become product priority.
3. Wire durable document/canvas persistence before treating editor/canvas as
   real workspace content.
4. Add deeper automated Review remediation executors for evidence repair,
   extraction retry, and structured candidate cleanup.
5. Continue tuning discovery scoring and digest write-back with real corpus
   acceptance data.
