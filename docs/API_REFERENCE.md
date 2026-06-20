# PSKA API Reference

Default local service:

```text
http://127.0.0.1:8765
```

If `PSKA_SERVICE_TOKEN` is configured, pass:

```http
Authorization: Bearer <token>
```

or:

```http
X-PSKA-Service-Token: <token>
```

## Status

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Process and database URL health |
| `GET /ready` | DB/schema/index/jobs/MCP/FastReAct readiness |
| `GET /index-status` | Canonical table counts |
| `GET /metrics` | Index, embedding, connector, and job metrics |

## Workspace

### `GET /workspace/today/data`

Read-only aggregation for the Today page.

Query:

```text
owner_user_id=user_primary
limit=10
```

Response sections:

- `continue_working`
- `discoveries`
- `needs_review`
- `system`

It currently composes existing backend methods and does not require new tables.

### `GET /workspace/corpus/data`

Read-only corpus explorer data.

Query:

```text
owner_user_id=user_primary
source_channel=files
query=agent runtime
limit=20
```

Returns sources, chunks, documents, entities, hyperedges, memories, and profile
cards.

### `POST /workspace/search/query`

Search for the PSKA Brain and workspace surfaces.

```json
{
  "query": "Agent Runtime",
  "mode": "direct",
  "capture": false,
  "user_id": "user_primary",
  "represented_user_id": "user_primary",
  "top_k": 5
}
```

`mode=agentic` uses the configured agentic service and falls back to direct
retrieval when unavailable.

### `POST /workspace/writer/suggest`

Read-only selected-text evidence suggestion.

```json
{
  "selected_text": "current block text",
  "draft_text": "draft text",
  "instruction": "请基于 PSKA 证据给出中文上下文提示。",
  "user_id": "user_primary",
  "represented_user_id": "user_primary",
  "top_k": 5
}
```

This endpoint does not mutate memory, profile, or graph.

## Review

| Endpoint | Purpose |
| --- | --- |
| `GET /review-items` | Raw review item list |
| `GET /console/reviews/data?status=pending&owner_user_id=user_primary&limit=50` | UI-ready pending review data |
| `POST /review-items/{id}/approve` | Approve a review item |
| `POST /review-items/{id}/reject` | Reject a review item |
| `POST /review-items/{id}/apply` | Apply an approved item |

Approve request:

```json
{
  "actor_user_id": "user_primary",
  "reason": "looks right",
  "apply": false
}
```

Review types include:

- `memory_candidate`
- `profile_update`
- `relationship_candidate`
- `conflict`
- `action_candidate`
- `low_confidence`
- `share_proposal`
- `sensitive_content`
- `entity_merge`

## Digest and Candidates

### `POST /digest/schedule`

Create digest backlog jobs from source items.

```json
{
  "owner_user_id": "user_primary",
  "limit": 20,
  "batch_size": 20,
  "force": false,
  "quota_window_seconds": 86400,
  "max_jobs_per_window": 24
}
```

### `GET /digest/batches/{job_id}`

Read scoped source/chunk context for digest workers.

### `POST /candidates`

Write grounded entities, hyperedges, review items, memory candidates, and
profile candidates.

Candidates require valid `source_refs` and preserve audit/source metadata.

## Jobs

| Endpoint | Purpose |
| --- | --- |
| `POST /jobs` | Submit job |
| `GET /jobs` | List jobs |
| `GET /jobs/{id}` | Job and events |
| `GET /jobs/stats` | Counts and digest backlog |
| `POST /jobs/{id}/lease` | Lease work |
| `POST /jobs/{id}/complete` | Complete job |
| `POST /jobs/{id}/fail` | Fail job |
| `POST /jobs/{id}/retry` | Retry job |
| `POST /jobs/{id}/cancel` | Cancel job |
| `POST /jobs/recover-stale` | Recover stale jobs |

## Connectors and Sources

| Endpoint | Purpose |
| --- | --- |
| `POST /ingest/channel-payload` | Ingest normalized channel payload |
| `POST /connectors/records` | Ingest connector record |
| `GET /connectors/states` | List connector states |
| `POST /connectors/states` | Upsert connector state |
| `GET /console/sources/data` | UI-ready source/connector summary |

## MCP

`POST /mcp` exposes PSKA tools over JSON-RPC for FastReAct or other clients.

Required tools include:

- `pska_search`
- `pska_index_status`
- `pska_job_context`
- `pska_write_candidates`
