# PSKA MVP Status

Date: 2026-06-10

## Executive Summary

PSKA has reached a runnable MVP for the initial private knowledge-base loop:

```text
Twitter/X archive zip
  -> pska.archive.v2 / channel payload
  -> PostgreSQL + pgvector schema
  -> source item / document / chunks
  -> LLM extraction
  -> entities / hyperedges / review items
  -> ACL-first retrieval
  -> LLM agentic planning and answer synthesis
  -> CLI / HTTP API / stdio + HTTP MCP
  -> Fastreact HTTP MCP tool access and digest worker
```

This is an MVP, not a production-complete PSKA. The important architectural
decision is now in place: extraction and agentic QA are LLM-required. There is
no rule-based fallback path for knowledge extraction or answer synthesis.
Provider/configuration/schema failures are surfaced as failures, with one
allowed recovery path: ask the LLM to repair its own JSON/schema output.

Current MVP scope is intentionally narrow: Twitter/X archive plus local
text-like files. The product work should now focus on the analysis/service loop
rather than adding many more connectors.

## Completion Snapshot

| Area | Status | Notes |
| --- | --- | --- |
| Data model | MVP complete | PostgreSQL schema covers users, teams, spaces, sources, documents, chunks, memory, profile cards, entities, hyperedges, review items, audit events. |
| Privacy model | MVP complete | Anonymous user/team IDs, private-first visibility, team-visible ACL fields, `agent_service` modeled separately. |
| Twitter/X channel | MVP complete | Extension and Python schema emit `pska.archive.v2`; legacy zip import remains compatibility-only. |
| Files connector | MVP first slice | `files-scan` ingests authorized local text-like files, records permission roots, metadata, content hash, and scan cursor. |
| Zip import | MVP complete | Imports current `~/Downloads/twitter_archive/*.zip`, preserves artifact paths, is idempotent by content hash. |
| Connector state | MVP functional | Connector records and durable connector states support enablement, scan cursor, sync status, permission scope, config, HTTP API, and CLI. |
| LLM extraction | MVP complete | Extracts entities, hyperedges, and review items through LLM JSON contract. |
| Hypergraph | MVP complete | Supports relation instances with multiple members and member roles; directionality is explicit. |
| Retrieval | MVP functional | ACL-first lexical/semantic placeholder ranking, citations, and one-hop hypergraph context. |
| Agentic search | MVP complete | LLM plans retrieval queries and synthesizes answers from retrieved evidence. |
| MCP boundary | MVP complete | PSKA exposes stdio MCP tools; Fastreact loads and calls them without importing PSKA internals. |
| HTTP API | MVP functional | Local API supports health/readiness, ingest, connector records/states, search, agentic search, jobs, review, candidates, digest schedule, and HTTP MCP. |
| Async jobs | Durable MVP | Jobs, events, lease/heartbeat, retry/backoff, stale recovery, job ops API/CLI, and Fastreact-backed digest/extraction contract are implemented. |
| E2E smoke | MVP complete | Real local smoke covers DB reset, zip import, LLM extraction, search, MCP, HTTP, and Fastreact MCP load. |
| Production readiness | Not complete | Foreground local daemon exists; still needs stronger review workflow, richer metrics, UI, system-level supervisor install, and real-data quality tuning. BGE-M3 embedding P0 is implemented but still needs production quality tuning. |

## Current Architecture

```mermaid
flowchart TD
    A["channels/twitter-x Chrome extension"] --> B["pska.archive.v2 zip"]
    B --> C["TwitterZipImporter"]
    C --> D["PostgresKnowledgeStore"]
    D --> E["source_items / documents / chunks"]
    E --> F["LLM ExtractionService"]
    F --> G["entities"]
    F --> H["hyperedges + members"]
    F --> I["review_items"]
    E --> J["RetrievalService"]
    G --> J
    H --> J
    J --> K["LLM AgenticSearchService"]
    K --> L["answer + citations + gaps + trace"]
    L --> M["CLI"]
    L --> N["HTTP API"]
    L --> O["stdio MCP server"]
    O --> P["Fastreact"]
    N --> Q["durable jobs + digest schedule"]
    Q --> P
    P --> R["candidate write-back"]
    R --> D
```

## LLM-Required Policy

PSKA now treats these operations as LLM-required:

- Entity extraction
- Hyperedge extraction
- Review item proposal
- Agentic retrieval planning
- Final answer synthesis

Allowed recovery:

- If the provider returns invalid JSON, PSKA asks the LLM to convert the same
  output into strict JSON.
- If JSON is valid but violates the PSKA schema, PSKA asks the LLM to reshape it
  to the required schema.

Disallowed recovery:

- No regex/rule-based extraction fallback.
- No local heuristic answer generation.
- No silent degradation to a fake answer when LLM configuration fails.

## Implemented Interfaces

CLI:

```bash
./scripts/pska mvp-bootstrap --twitter-archive ~/Downloads/twitter_archive --notes-root ~/Documents/notes --extract
./scripts/pska mvp-status --summary
./scripts/pska local-daemon
./scripts/pska db-reset --name pska_smoke
./scripts/pska --database-url postgresql:///pska_smoke import-twitter-zips
./scripts/pska --database-url postgresql:///pska_smoke extract-all
./scripts/pska --database-url postgresql:///pska_smoke search --query "GitHub"
./scripts/pska --database-url postgresql:///pska_smoke agentic-search --query "GitHub"
./scripts/pska --database-url postgresql:///pska_smoke serve --port 8766
./scripts/pska --database-url postgresql:///pska_smoke digest-schedule --owner-user-id user_primary
./scripts/pska --database-url postgresql:///pska_smoke review-list --status pending --summary
PYTHONPATH=src ../.pska/venvs/pska-py312/bin/python scripts/current_sample_gate.py --database-url postgresql:///pska --require-graph --require-review-or-memory
```

HTTP:

- `GET /health`
- `GET /index-status`
- `GET /review-items`
- `POST /ingest/channel-payload`
- `POST /search`
- `POST /agentic-search`
- `POST /extract/all`
- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `POST /digest/schedule`
- `GET /digest/batches/{job_id}`
- `POST /candidates`
- `POST /mcp`

MCP tools:

- `pska_search`
- `pska_agentic_search`
- `pska_index_status`
- `pska_ingest_channel_payload`
- `pska_extract_all`
- `pska_review_items`
- `pska_job_context`
- `pska_write_candidates`

Fastreact sees these as namespaced tools, for example:

- `pska_pska_search`
- `pska_pska_agentic_search`
- `pska_pska_index_status`

## Verified Test Evidence

Unit and contract tests:

```bash
cd "/Users/xudawei/Documents/personal archive/core"
python3 -m pytest -q
# 169 passed

cd "/Users/xudawei/Documents/personal archive/channels/twitter-x"
python3 -m pytest -q
# 9 passed
```

Full real smoke:

```bash
cd "/Users/xudawei/Documents/personal archive/core"
.pska/venvs/pska-py312/bin/python core/scripts/e2e_smoke.py
```

Current Postgres sample gates:

- `postgresql:///pska`: current daemon-aligned sample; passes strict MVP+ gate with source/chunk/search/citations/digest jobs/graph grounding/review-or-memory.
- `postgresql:///pska_mvp_plus_sample`: small Twitter/files sample; currently passes retrieval and graph grounding after real LLM extraction, with digest worker write-back still pending.

MVP+ limited-data object-level gate:

```bash
cd "/Users/xudawei/Documents/personal archive/core"
PYTHONPATH=src ../.pska/venvs/pska-py312/bin/python scripts/mvp_plus_smoke.py
```

MVP+ limited-data HTTP service gate:

```bash
cd "/Users/xudawei/Documents/personal archive/core"
PYTHONPATH=src ../.pska/venvs/pska-py312/bin/python scripts/mvp_plus_http_smoke.py
```

MVP+ limited real Twitter/X archive gate:

```bash
cd "/Users/xudawei/Documents/personal archive/core"
PYTHONPATH=src ../.pska/venvs/pska-py312/bin/python scripts/mvp_plus_real_sample_smoke.py --limit 3
```

Use `--skip-llm` to validate Postgres reset, limited zip import, search, and
digest backlog creation without calling the LLM. Without `--skip-llm`, the
script also runs real LLM extraction and agentic search using
`PSKA_LLM_API_KEY_FILE` or `~/api_key.txt`.

Latest local evidence:

- `--limit 3 --skip-llm`: passed with 3 real archive zips, imported sources,
  chunks, cited search, and queued digest job.
- `--limit 1`: passed with real LLM extraction, entities, hyperedges, graph
  retrieval context, agentic answer synthesis, and queued digest job.
- After Postgres hyperedge source-ref deserialization was fixed, real GraphRAG
  paths include `source_refs` and `evidence_citations`, with no ungrounded graph
  edge diagnostic in the 1-zip gate.
- The real-sample smoke now asserts `search_has_citations`, and in LLM mode also
  asserts `graph_has_evidence_citations`.

This deterministic smoke uses a tiny in-memory dataset and fake LLM responses
to verify the strengthened loop before running real archives. The object-level
gate validates the Python service components directly; the HTTP gate validates
the online service routes and HTTP MCP transport:

- limited source ingest
- LLM extraction contract
- Fastreact-style candidate write-back
- durable digest job backlog creation
- grounded GraphRAG path retrieval
- memory/profile context with citations
- conflict and sensitivity diagnostics
- agentic QA from cited retrieval
- MCP `pska_search`

The real smoke currently verifies:

- `pska_smoke` database reset and migration
- Import of all current `~/Downloads/twitter_archive/*.zip`
- LLM extraction into entities and hyperedges
- CLI search with citations and hypergraph context
- Direct stdio MCP `pska_search`
- CLI `agentic-search` with LLM planning and answer synthesis
- HTTP `/health` and `/agentic-search`
- Fastreact loading PSKA MCP tools and calling `pska_pska_search`

Direct document-to-graph-to-agentic-QA demo:

```bash
cd "/Users/xudawei/Documents/personal archive/core"
PYTHONPATH=src PSKA_LLM_API_KEY_FILE="$HOME/api_key.txt" python3 scripts/document_graph_qa_demo.py
```

Observed result:

- A planning note becomes a source item and document.
- LLM extracts entities such as `Project Atlas`, `P-204`, `dependent K`,
  `Twitter Archive channel`, and `Review Agent`.
- LLM extracts hyperedges including `covers` and `depends_on`.
- Agentic search answers: `Policy P-204 covers dependent K during education enrollment.`
- The answer includes a citation back to `Team Planning Note`.

## Known Limitations

These are intentional MVP limits, not hidden completions:

- Embeddings are still placeholder/deterministic enough for tests; production
  semantic search needs a real embedding provider and backfill jobs.
- Retrieval ranking is still `lexical_rrf_placeholder`; reranking is not yet
  LLM/cross-encoder quality.
- LLM extraction is synchronous; a real deployment needs a job queue, retry
  policy, task status, and partial failure reporting.
- Review items exist as data records, but approval workflow is not yet a full
  product surface.
- HTTP API is local and simple; it is not yet an authenticated production
  service.
- Conversation ingestion is modeled but not yet wired to a real chat log
  collector.
- User profile cards and agent memories exist in the model; automatic promotion,
  decay, and user-owned memory review still need deeper implementation.
- Audit tables exist, but not every write path is fully audit-instrumented.
- No UI for browsing sources, graph, review queue, or profile memory yet.

## MVP Definition Of Done

The initial PSKA MVP can be considered functionally complete when all of these
remain true:

- A channel can produce canonical source archives.
- PSKA can ingest those archives into Postgres.
- PSKA can preserve original artifacts and return citations.
- LLM extraction can create entities, hyperedges, and review proposals.
- Retrieval can combine document chunks and graph context under ACL.
- Agentic search can plan, retrieve, check evidence, and synthesize an answer.
- Fastreact can access PSKA through MCP without importing PSKA internals.
- Tests and smoke can prove the loop end to end.

Current status: this MVP definition is met.

Detailed next-stage TODOs are tracked in [`roadmap-todo-zh.md`](roadmap-todo-zh.md).

## Next Stage Priorities

1. Real embedding pipeline

   First pass complete with local BGE-M3, 1024-dimensional pgvector storage,
   batch backfill, query embeddings, vector search, and RRF merge. Next work is
   real-data tuning, reranking, and report-level quality tracking.

2. Async ingestion and extraction jobs

   Convert zip import and LLM extraction from synchronous CLI work into durable
   jobs with retry, status, and error inspection.

3. Review and approval workflow

   Implement approval decisions for sharing, sensitive profile updates, memory
   promotion, entity merges, and deletes. Every decision should write audit
   events.

4. Conversation source ingestion

   Add conversation/message source items so long-term PSKA memory is not limited
   to files and Twitter/X archives.

5. Profile card and agent memory management

   Add LLM-driven proposals, confidence updates, stale memory handling, and
   user-owned review controls.

6. Retrieval quality upgrade

   Add real hybrid retrieval, reranking, conflict search, file lookup, and
   agentic multi-step search policies.

7. Local UI

   Build a private local console for sources, extracted graph, citations,
   review queue, memory cards, and system health.

## Operational Notes

LLM configuration:

```bash
export PSKA_LLM_API_KEY_FILE="$HOME/api_key.txt"
```

The key file may contain:

```text
api-key
model-name
base-url
```

Do not commit real keys, real user names, home paths, private aliases, or
personal identifiers into repository config or examples.
