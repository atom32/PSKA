# PSKA Feature Reality Check

This document separates shipped behavior from prototype presentation. Use it
when deciding whether a workspace surface is ready for real use, needs backend
connection, or is only a design placeholder.

Status legend:

- `Real`: backed by persistent data and working API behavior.
- `Partial`: some real data or API support exists, but the workflow is not end
  to end.
- `Mock`: visible in the UI, but powered by local sample data or static state.
- `Planned`: product/API concept exists, but no usable surface yet.

## Current Snapshot

As of the latest local inspection, the `pska` database is not empty. It contains
real PSKA schema data plus development fixtures and smoke-test data:

| Area | Current State |
| --- | --- |
| Sources/Documents | 36 source items, 36 documents |
| Chunks | 147 chunks |
| Embeddings | 147 embedded chunks with BGE-M3 (`BAAI/bge-m3`, 1024 dimensions) |
| Entities | 348 entities |
| Hyperedges | 146 graph relationships |
| Review Items | 20 historical items, 0 pending items |
| Agent Memories | 10 memories |
| Profile Cards | 4 profile cards |
| Jobs | 13 digest jobs, 7 succeeded and 6 failed |
| Source Mix | `files`, `mock_mvp_plus`, `manual`, `pska_agent`, `manual_canary`, `pska_briefing` |

The database proves that the ingestion, graph extraction, review, memory,
profile, audit, and job models exist. It does not yet prove high-quality recall,
because the local corpus currently has no populated chunk embeddings.

## Product Surface Matrix

| Feature | Backend | Frontend | Reality | Notes |
| --- | --- | --- | --- | --- |
| One-command local startup | Real | Real | Real | `./start.sh` starts the backend supervisor and Vite frontend. |
| Today aggregation | Real | Real | Partial | `GET /workspace/today/data` returns real sections, but it composes existing tables instead of a dedicated Today model. |
| Continue Working | Partial | Real | Partial | Uses recent source/document data. There is no workspace activity model for opened/edited/pinned surfaces. |
| Discovery Feed | Partial | Real | Partial | Currently wraps existing pending review items and hyperedges. Hyperedge-backed items are graph browsing, not newly produced discoveries. |
| Needs Review | Real | Real | Partial | Review APIs work, but current DB has 0 pending items. Frontend must not show fallback review cards when real API returns an empty list. |
| Review approve/reject | Real | Real | Real | `POST /review-items/{id}/approve` and `/reject` are wired from Today for real review ids. |
| Review apply | Real | Partial | Partial | Backend supports `/apply`; Today uses `approve` with `apply=true` for recommended apply actions. A full Review page is not built. |
| Discovery accept/ignore | Planned | Partial | Mock/Partial | Works only when a discovery has a `review_item_id`; hyperedge discoveries have no accept/ignore/snooze persistence. |
| Snooze/later | Planned | Mock | Mock | `稍后` only updates local UI state. No backend postponement model exists. |
| PSKA Brain related knowledge | Real | Real | Partial | Calls `POST /workspace/search/query`; falls back to local analysis if unauthorized or unavailable. Recall quality is limited without embeddings. |
| PSKA Brain entities | Real | Real | Partial | Uses graph paths/search/corpus where available; initial state is static sample data. |
| PSKA Brain timeline | Partial | Partial | Partial | Can load sources from corpus context, but there is no dedicated historical note timeline model. |
| Suggested connections | Partial | Partial | Partial | Can show graph paths/hyperedges, but no connection creation workflow is wired. |
| Document editor | Planned | Mock | Mock | Tiptap editor exists, but content is local Zustand state and is not saved to documents/chunks. |
| Document toolbar | Mock | Real | Partial | Formatting controls work in the local editor only. No backend persistence. |
| Canvas | Planned | Mock | Mock | React Flow canvas is static sample nodes/edges. No canvas persistence or node model is connected. |
| Corpus browser | Real | Planned | Partial | Backend `GET /workspace/corpus/data` exists. Frontend only uses it to enrich Brain panels; no full corpus page exists. |
| Graph browser | Real | Planned | Partial | Hyperedges and members exist, but the frontend has no real graph exploration surface. |
| Search page | Real | Planned | Partial | Search API exists and Brain uses it. Left-nav Search is not implemented as a page. |
| Project/tag navigation | Planned | Mock | Mock | Sidebar labels exist; no project/tag data or navigation route is wired. |
| Service token field | Real | Real | Real | Token is stored in `sessionStorage` and sent as `Authorization: Bearer`. |
| Telemetry | Planned | Planned | Planned | `docs/TELEMETRY.md` defines events, but no frontend/backend event writes are connected. |

## API Reality Matrix

| API | Reality | Used By Frontend | Notes |
| --- | --- | --- | --- |
| `GET /health` | Real | No | Operational health endpoint. |
| `GET /ready` | Real | No | Shows readiness; current local readiness may report schema/metrics/agentic-service warnings. |
| `GET /index-status` | Real | No | Canonical table counts. |
| `GET /metrics` | Real | No | Includes index, connector, embedding, and jobs metrics. |
| `GET /workspace/today/data` | Real | Yes | Today source of truth. Aggregated, not dedicated domain model. |
| `GET /workspace/corpus/data` | Real | Yes | Used to populate Brain entities/timeline/connections outside Today. |
| `POST /workspace/search/query` | Real | Yes | Used by Brain context analysis. Direct mode works without agentic service. |
| `POST /workspace/writer/suggest` | Real | No | Backend exists; frontend editor does not expose it yet. |
| `GET /review-items` | Real | No | Raw review list. |
| `GET /console/reviews/data` | Real | No | UI-ready review queue, not yet wired as a frontend page. |
| `POST /review-items/{id}/approve` | Real | Yes | Today calls this for real review ids. |
| `POST /review-items/{id}/reject` | Real | Yes | Today calls this for real review ids. |
| `POST /review-items/{id}/apply` | Real | Partial | Backend exists; Today currently folds apply into approve where possible. |
| `POST /digest/schedule` | Real | No | Candidate production entry point; not exposed in workspace UI. |
| `POST /candidates` | Real | No | Writes grounded entities/hyperedges/review/memory/profile candidates. |
| Jobs endpoints | Real | No | Worker/admin surface exists at API level. |
| Connector/source endpoints | Real | No | Used by backend/CLI flows, not workspace UI. |

## Button Reality Matrix

| UI Control | Reality | Behavior |
| --- | --- | --- |
| Today refresh | Partial | Resets local Brain status; Today query refetch is handled by React Query load lifecycle rather than this button directly. |
| Continue Working item | Mock/Partial | Opens local document/canvas mode. It does not open the selected source/document id. |
| Discovery `接受` | Partial | Calls review approve only if the item has `review_item_id`; otherwise local mark only. |
| Discovery `忽略` | Partial | Calls review reject only if the item has `review_item_id`; otherwise local mark only. |
| Discovery `稍后` | Mock | Local mark only. |
| Review `批准` | Real for real ids | Calls backend approve. Fails if the displayed card is fallback mock. |
| Review `批准并应用` | Partial | Calls approve with `apply=true`. Needs clearer API/UI contract. |
| Review `拒绝` | Real for real ids | Calls backend reject. Fails if the displayed card is fallback mock. |
| Review `稍后` | Mock | Local mark only. |
| Document formatting buttons | Real locally | Mutate Tiptap editor state only. |
| Canvas controls | Real locally | React Flow pan/zoom works on static sample graph. |
| Sidebar Corpus/Project/Tags/Search/Review | Mock | Buttons exist without route/page behavior. |

## Known Truth Gaps

### Embedding Recall

The embedding pipeline exists in code, including disabled/BGE-M3 providers,
backfill helpers, and offline index state tracking. The inspected local database
now has all 147 chunks embedded with BGE-M3 (`BAAI/bge-m3`, 1024 dimensions),
and search results expose vector recall through `source: "vector"` plus vector
score diagnostics.

Reality: `Real` for the current local corpus.

Next step: add a readiness card that reports embedded chunk count versus total
chunk count, and keep retrieval quality replay tracking vector/lexical/graph
hits separately.

### Discovery

The UI label says `Discoveries`, but the current feed mostly wraps existing
hyperedges and pending review items. A true discovery feed needs freshness,
novelty, lifecycle state, and action persistence.

Reality: `Partial`.

Next step: introduce a `DiscoveryItem` or equivalent view over candidate/review
production with states such as `new`, `accepted`, `ignored`, and `snoozed`.

### Continue Working

Today uses recent source items as a proxy for work activity. PSKA does not yet
know which document/canvas/search/review the user opened, edited, pinned, or
viewed.

Reality: `Partial`.

Next step: add a workspace activity model before presenting Continue Working as
true work resumption.

### Editor and Canvas Persistence

The workspace looks like a writing/canvas environment, but these surfaces are
not connected to PSKA documents, nodes, or graph relationships.

Reality: `Mock`.

Next step: define the minimal persisted node/document model and wire open/save
before adding advanced editing behavior.

## Highest Priority Fixes

1. Stop showing Today fallback cards when the backend returned a valid empty
   section. Empty real data should render as empty state, not mock data.
2. Add a visible development badge for `Mock`, `Partial`, and `Real` sections in
   the frontend while the product is still being wired.
3. Backfill or explicitly disable embedding recall in the UI readiness panel.
4. Add a real Review page using `GET /console/reviews/data`.
5. Add discovery lifecycle persistence before presenting hyperedges as
   actionable discoveries.
6. Add workspace activity tracking for Continue Working.
