# PSKA Frontend Backend Feature Map

This document maps the current frontend prototype to existing PSKA backend
capabilities. It is intentionally practical: what exists, what endpoint or CLI
to use, and what is still missing.

## Summary

The backend already supports most of the data needed for a real Today page:

- Continue Working: partially available through recent sources and workspace
  corpus data, but there is no dedicated "recent workspace activity" model yet.
- Discoveries: available indirectly through digest jobs, candidate write-back,
  review items, entities, hyperedges, and search evidence. There is no dedicated
  "discovery feed" endpoint yet.
- Needs Review: available now through review APIs and console review data.

The first read-only Today aggregation endpoint now exists:
`GET /workspace/today/data`. It combines existing backend data into
frontend-ready sections without adding new tables.

## Existing HTTP Surfaces

All endpoints are served by `./scripts/pska serve --port 8765`. If
`PSKA_SERVICE_TOKEN` is set, pass either:

```http
Authorization: Bearer <token>
```

or:

```http
X-PSKA-Service-Token: <token>
```

### Service / Health

| Need | Endpoint | Status |
| --- | --- | --- |
| Is PSKA online? | `GET /health` | Implemented |
| Is DB/schema/MCP/jobs ready? | `GET /ready` | Implemented |
| Counts for sources/chunks/entities/jobs | `GET /index-status`, `GET /metrics` | Implemented |

### Today / Daily Entry

| Need | Endpoint | Status |
| --- | --- | --- |
| Daily source/review/job summary | `GET /console/data?owner_user_id=user_primary&limit=5` | Implemented |
| Frontend-ready Today feed | `GET /workspace/today/data?owner_user_id=user_primary&limit=10` | Implemented |

`/console/data` already returns useful fields:

- `source_counts`
- `digest_backlog`
- `pending_reviews`
- `failed_jobs`
- `source_summary.recent_sources`
- `recommended_commands`
- `deterministic_next_actions`

The Today endpoint currently composes this data into a workspace-facing shape.
The lower-level `/console/data` JSON remains useful for admin/debug views.

### Continue Working

| Need | Endpoint | Status |
| --- | --- | --- |
| Recent source material | `GET /console/sources/data?owner_user_id=user_primary&limit=20` | Implemented |
| Corpus/document/chunk browser | `GET /workspace/corpus/data?limit=20` | Implemented |
| Recent edited workspace docs/canvases | None | Missing |

Current backend can show recent sources, documents, chunks, entities,
hyperedges, memories, and profiles. It does not yet track frontend-native
workspace activity such as "last opened document", "last edited canvas", or
"pinned project".

### Discoveries

| Need | Endpoint | Status |
| --- | --- | --- |
| Schedule digest over new source backlog | `POST /digest/schedule` | Implemented |
| Digest worker reads scoped material | `GET /digest/batches/{job_id}` | Implemented |
| Worker writes discovered entities/relations/review items | `POST /candidates`, `POST /digest/candidates` | Implemented |
| Show discoveries as a user-facing feed | `GET /workspace/today/data` | Implemented v0 |

Discovery is currently represented by backend primitives rather than one UI
feed:

- extracted `entities`
- `hyperedges`
- `review_items`
- `memory_candidates`
- `profile` candidates
- digest jobs and job events

The v0 discovery feed is derived from pending review items plus recent
hyperedges. A dedicated discovery model/feed can wait until real usage proves
the need.

### Needs Review

| Need | Endpoint | Status |
| --- | --- | --- |
| Pending review queue | `GET /console/reviews/data?status=pending&owner_user_id=user_primary&limit=50` | Implemented |
| Raw review item list | `GET /review-items` | Implemented |
| Approve item | `POST /review-items/{review_item_id}/approve` | Implemented |
| Reject item | `POST /review-items/{review_item_id}/reject` | Implemented |
| Apply approved item | `POST /review-items/{review_item_id}/apply` | Implemented |

Review item types supported by the backend include:

- `share_proposal`
- `sensitive_content`
- `profile_update`
- `entity_merge`
- `conflict`
- `memory_candidate`
- `relationship_candidate`
- `action_candidate`
- `low_confidence`

Relationship candidates already have a grounded apply path: missing source refs
cannot be applied, and successful apply creates a hyperedge with evidence and
audit metadata.

### Search / PSKA Brain

| Need | Endpoint | Status |
| --- | --- | --- |
| Ask PSKA unified QA | `POST /workspace/ask` with `intent=auto\|quick\|deep` | Implemented |
| Ask PSKA streaming | `POST /workspace/ask/stream` SSE | Implemented |
| Legacy direct retrieval | `POST /workspace/search/query` with `mode=direct` | Compatibility/debug |
| Legacy agentic search with direct fallback | `POST /workspace/search/query` with `mode=agentic` | Compatibility/debug |
| Corpus data for entities/edges/memory/profile | `GET /workspace/corpus/data` | Implemented |
| Selected-text evidence suggestion | `POST /workspace/writer/suggest` | Implemented |

Main product surfaces should call `/workspace/ask`. It returns `answer`,
`route`, `evidence`, `citations`, `source_refs`, `trace`, and `timing`.
`route.retrieval_owner` is either `pska` for quick PSKA-owned retrieval or
`fastreact_pska_mcp` for deep retrieval owned by FastReAct through PSKA read-only
MCP tools. `/workspace/search/query` remains for older clients and diagnostics.

Ask evidence may include:

- `citations`
- `source_refs`
- `results`
- `graph_paths`
- `memory_context`
- `profile_context`
- `gaps`
- `conflicts`

`/workspace/writer/suggest` is read-only and explicitly does not mutate memory,
profile, or graph.

### Jobs / Digest Ops

| Need | Endpoint | Status |
| --- | --- | --- |
| Job list/stats | `GET /jobs`, `GET /jobs/stats` | Implemented |
| Console job overview | `GET /console/jobs/data?limit=20` | Implemented |
| Submit job | `POST /jobs` | Implemented |
| Lease/complete/fail/retry/cancel/recover | `/jobs/*` endpoints | Implemented |
| Schedule digest backlog | `POST /digest/schedule` | Implemented |

Digest scheduling creates `digest_via_fastreact` jobs. PSKA owns scheduling,
scope, source refs, job state, retry/backoff, quota policy, and candidate
write-back. FastReAct owns the agentic digest loop.

## Existing CLI Surfaces

Useful commands for manual operation and debugging:

```bash
./scripts/pska daily-status
./scripts/pska daily-briefing --owner-user-id user_primary --limit 5
./scripts/pska daily-briefing --owner-user-id user_primary --limit 5 --narrative
./scripts/pska review-list --status pending --owner-user-id user_primary --limit 20 --summary
./scripts/pska review-approve <review_item_id> --reason "looks right"
./scripts/pska review-reject <review_item_id> --reason "not grounded enough"
./scripts/pska review-apply <review_item_id>
./scripts/pska memory-list --owner-user-id user_primary --limit 20
./scripts/pska profile-list --owner-user-id user_primary --limit 20
./scripts/pska digest-schedule --owner-user-id user_primary --limit 20
./scripts/pska jobs stats
./scripts/pska files-sync
./scripts/pska search --query "Agent Runtime"
./scripts/pska agentic-search --query "Agent Runtime" --capture
```

## Recommended Next Backend Endpoint

Implemented read-only endpoint:

```text
GET /workspace/today/data?owner_user_id=user_primary&limit=10
```

Suggested response:

```json
{
  "ok": true,
  "owner_user_id": "user_primary",
  "continue_working": [
    {
      "id": "src_xxx",
      "type": "source",
      "title": "PSKA Product Design",
      "subtitle": "files / updated recently",
      "opened_surface": "document",
      "source_refs": [{"source_item_id": "src_xxx"}]
    }
  ],
  "discoveries": [
    {
      "id": "rev_xxx",
      "type": "relationship_candidate",
      "label": "发现关联",
      "title": "FastReAct ↔ Tool Runtime",
      "summary": "候选、审核、合并模式重复出现。",
      "confidence": 0.86,
      "evidence_count": 3,
      "review_item_id": "rev_xxx"
    }
  ],
  "needs_review": [
    {
      "review_item_id": "rev_xxx",
      "review_type": "memory_candidate",
      "title": "PSKA 不自动写入正文",
      "summary": "建议进入长期记忆。",
      "confidence": 0.9,
      "recommended_action": "approve_or_reject",
      "source_ref_status": "present"
    }
  ],
  "system": {
    "digest_backlog": {"jobs": 1, "source_items": 20},
    "failed_jobs": {"count": 0},
    "source_counts": {"source_items": 24, "chunks": 135}
  }
}
```

Current implementation composes existing backend methods:

- `console_dashboard`
- `console_sources`
- `console_reviews`
- `workspace_corpus`
- `job_stats`

No new storage is required for v0. The frontend Today page calls this endpoint
and falls back to mock data if the service is unavailable or unauthorized.

Today review actions call existing backend endpoints:

- `批准` calls `POST /review-items/{id}/approve`
- `批准并应用` calls `POST /review-items/{id}/approve` with `apply=true`
- `拒绝` calls `POST /review-items/{id}/reject`

Discovery cards only call the backend when they include a `review_item_id`.
Discoveries derived from existing hyperedges do not have a review item to
mutate, so `接受` / `忽略` are local UI state for now. `稍后` is also local UI
state because there is not yet a snooze/postpone backend model.

## Gaps Before Today Can Stop Using Mock Data

1. Decide how to derive `continue_working` before native workspace history
   exists. v0 can use recent sources.
2. Decide how to derive `discoveries`. v0 can use pending review items of type
   `relationship_candidate`, `conflict`, `memory_candidate`, and recent graph
   edges.
3. Add backend support for snooze/postpone if `稍后` should persist.
4. Add `WorkspaceActivity` if Continue Working needs to mean "what the user was
   actively doing", not just "recent source material".
5. Keep `Insight` out of v0. Do not add a separate endpoint until real digest
   data proves the need.
