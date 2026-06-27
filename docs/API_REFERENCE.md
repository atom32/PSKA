# PSKA API Reference

Default local service:

```text
http://127.0.0.1:8765
```

If `.pska/config.json` sets `service.service_token`, pass:

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
- `discoveries` from the score-filtered discovery feed
- `needs_review`
- `system`

It composes backend methods for activity, discovery, review, corpus, jobs, and
readiness.

### `GET /workspace/discoveries/data`

Producer-backed discovery feed.

Query:

```text
owner_user_id=user_primary
limit=50
min_score=0.5
```

Response:

- `discoveries`: recent `new` `DiscoveryItem` records whose
  `discovery_score >= min_score`
- `count`: returned discovery count
- `total_new`: recent `new` discovery count before score filtering
- `window_days`: freshness window, currently 7

Each discovery includes `fingerprint`, `evidence_snapshot`,
`discovery_score`, and `quality_signals`. Low-scoring producer output remains
persisted for audit/debugging, but Today should not show it by default.

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

### `POST /workspace/ask`

Primary Ask PSKA endpoint for workspace QA. This is the product-facing route
used by Today, Corpus, and Graph surfaces.

```json
{
  "query": "Agent Runtime",
  "intent": "auto",
  "surface": "today",
  "scope": {},
  "session_id": "optional-session-id",
  "user_id": "user_primary",
  "represented_user_id": "user_primary",
  "top_k": 8
}
```

`intent=quick` lets PSKA own retrieval. `intent=deep` delegates retrieval to
FastReAct through PSKA read-only MCP tools. `intent=auto` first runs the PSKA
planner (`route.routing_owner=pska_planner`) to extract query terms and choose
between those routes. `route.retrieval_owner` is still the hard evidence owner:
either `pska` or `fastreact_pska_mcp`.

Response includes:

- `answer`
- `route`
- `evidence`
- `citations`
- `source_refs`
- `agent_steps`
- `trace`
- `timing.total_ms`
- `timing.time_to_first_answer_ms`
- `timing.time_to_first_agent_event_ms`

`agent_steps` is the product-safe agentic search timeline. Quick answers include
PSKA planner and GraphRAG steps such as understanding, route selection, searching,
reading results, and forming the answer. Deep answers start with the PSKA planner
route step, then include translated FastReAct events. `trace.events` may retain
raw FastReAct events for diagnostics, but product UI must keep them behind a
debug foldout and must not copy them into the answer.

### `POST /workspace/ask/stream`

SSE version of Ask PSKA. Events are `route`, `agent_step`, `evidence`,
`answer_delta`, `trace`, `done`, and `error`. `agent_step` is safe to show as a
user-facing search timeline. `time_to_first_agent_event_ms` starts at the first
`agent_step`; `time_to_first_answer_ms` starts when the first user-visible
answer character is emitted, not when route, agent, or trace events start.

### `POST /workspace/search/query`

Compatibility/debug search endpoint for legacy clients and eval tools. Product
UI should prefer `/workspace/ask`.

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
retrieval when unavailable. New product surfaces should not expose this mode
switch to users.

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

### Writing Workspace / Inquiry Graph

Tenant-scoped writing boards persist a question-answer network used to organize
Ask PSKA results into document drafts. Writing APIs do not replace Ask PSKA:
question nodes still call `POST /workspace/ask/stream` with
`surface="writing"` and `scope={board_id,node_id}`.

Core endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /workspace/writing/boards` | List current user's writing boards. |
| `POST /workspace/writing/boards` | Create a board with `title`, `goal`, and optional metadata. |
| `GET /workspace/writing/boards/{board_id}` | Load one board with nodes and edges. |
| `PATCH /workspace/writing/boards/{board_id}` | Update board title, goal, or metadata. |
| `POST /workspace/writing/boards/{board_id}/nodes` | Create a `goal`, `question`, `answer`, `evidence`, `gap`, `section`, or `draft` node. |
| `PATCH /workspace/writing/boards/{board_id}/nodes/{node_id}` | Update node title, Markdown body, position, status, citations, source refs, or metadata. |
| `DELETE /workspace/writing/boards/{board_id}/nodes/{node_id}` | Delete a node and its connected edges. |
| `POST /workspace/writing/boards/{board_id}/edges` | Create a typed relation such as `answered_by`, `supported_by`, `raises`, or `included_in`. |
| `DELETE /workspace/writing/boards/{board_id}/edges/{edge_id}` | Delete one relation. |
| `POST /workspace/writing/boards/{board_id}/suggest-questions` | Return generic follow-up question suggestions; does not persist nodes. |
| `POST /workspace/writing/boards/{board_id}/compose` | Build Markdown from selected answer nodes; does not perform retrieval. |

`compose` is intentionally retrieval-free. It only uses the selected answer
nodes' Markdown, citations, and source refs, so the retrieval owner remains the
original Ask PSKA run that produced those answer nodes.

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

## Knowledge Sources and Connector Runtime

| Endpoint | Purpose |
| --- | --- |
| `POST /ingest/channel-payload` | Ingest normalized channel payload |
| `POST /connectors/records` | Ingest connector record |
| `GET /connectors/states` | List adapter runtime states |
| `POST /connectors/states` | Upsert adapter runtime state |
| `GET /console/sources/data` | UI-ready source/runtime summary |

Knowledge Source is the user-facing model. Connector state endpoints remain as
runtime/adapter support for sync cursors, manifests, and diagnostics.

## MCP

`POST /mcp` exposes PSKA tools over JSON-RPC for FastReAct or other clients.

Required tools include:

- `pska_search`
- `pska_index_status`
- `pska_job_context`
- `pska_write_candidates`
