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

The frontend is a User Workspace scaffold. It has real Today/Discoveries/
Corpus/Search-backed surfaces and graph/review-adjacent panels, but it is not a
complete file manager or Knowledge Sources UI yet.

## Product Surface Matrix

| Feature | Backend | Frontend | Reality | Notes |
| --- | --- | --- | --- | --- |
| One-command local startup | Real | Real | Real | `./start.sh` starts the backend supervisor and Vite frontend. |
| Folder source sync | Real | Partial | Partial | CLI/backend source model exists; frontend management UI is not complete. |
| Twitter/X archive import | Real | Partial | Partial | `files-sync` and `digest-now` import the workspace archive inbox by content hash. |
| Today aggregation | Real | Real | Partial | Uses real `/workspace/today/data`; empty real sections should render empty states. |
| Discovery feed | Real | Real | Partial | Persistent discoveries and score filtering exist; quality still depends on producers and corpus. |
| Needs Review | Real | Real | Partial | Review APIs and fallback digest review exist; full Review workspace is still evolving. |
| Review approve/reject/apply | Real | Partial | Partial | Backend supports review actions; frontend coverage is not complete. |
| PSKA Brain search | Real | Real | Partial | Uses workspace search and corpus context; recall quality depends on indexing/embeddings. |
| Corpus browser | Real | Partial | Partial | Backend corpus endpoint exists; frontend uses it in selected panels. |
| Graph browser | Real | Partial | Partial | Graph data exists; visual exploration is still early. |
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

1. Build a Knowledge Sources/file management UI that shows watched folders,
   last sync, sync report, and unmonitored workspace folders.
2. Add user-facing "why not found?" diagnostics across search, source, and CLI.
3. Complete Review workspace flows for accept/reject/apply/snooze with clear
   backend persistence.
4. Wire durable document/canvas persistence before treating editor/canvas as
   real workspace content.
5. Continue tuning discovery scoring and digest write-back with real corpus
   acceptance data.
