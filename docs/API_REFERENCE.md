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

### Knowledge Bases / Corpus Scope

`knowledge_bases` is the first-class corpus boundary for multi-KB RAG. Product
UI may label this surface as `资料库`, but API clients should treat
`knowledge_base_id` as the stable retrievable-corpus id. `KnowledgeSource`
remains the connector/source configuration object.

Core endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /workspace/knowledge-bases?include_archived=false` | List current user's active KBs and ensure a default KB exists. |
| `POST /workspace/knowledge-bases` | Create a KB. Required: `name`; optional: `description`, `slug`, `kb_type`, `visibility`, `default_space_id`, `config`. |
| `GET /workspace/knowledge-bases/{knowledge_base_id}` | Load one KB, including counts/readiness and source item ids. |
| `PATCH /workspace/knowledge-bases/{knowledge_base_id}` | Update name, slug, description, type, visibility, pinned state, config, readiness, or status. |
| `DELETE /workspace/knowledge-bases/{knowledge_base_id}` | Archive a KB. This removes it from default lists but does not hard-delete source items. |
| `POST /workspace/knowledge-bases/{knowledge_base_id}/restore` | Restore an archived KB. |
| `POST /workspace/knowledge-bases/{knowledge_base_id}/pin` | Pin a KB. |
| `DELETE /workspace/knowledge-bases/{knowledge_base_id}/pin` | Unpin a KB. |
| `POST /workspace/knowledge-bases/search` | Search one or more KBs and return attributed results/citations. |

List/detail responses include `knowledge_bases`, `default_knowledge_base_id`,
and each KB's `counts` and `readiness`. The current minimal readiness contract
includes source/document/chunk counts, active/embedded chunk counts,
`embedding_coverage`, `retrieval_ready`, and processing/error counts.

Legacy data migration creates one default KB per `(tenant_id, owner_user_id)` and
backfills existing `knowledge_sources` and `source_items` into membership tables.
The migration is idempotent; source/item membership is not stored as a single
column on the source item.

Endpoints that create or ingest corpus material accept `knowledge_base_id`:

| Endpoint | Scope field |
| --- | --- |
| `POST /workspace/sources/text` | JSON `knowledge_base_id` |
| `POST /workspace/sources/upload` | JSON/form `knowledge_base_id` |
| `POST /workspace/sources` | JSON `knowledge_base_id` |
| `POST /workspace/sources/sync` | JSON `knowledge_base_id` or existing source membership |
| `POST /files/sync` | JSON `knowledge_base_id` |

When no KB id is passed, the request remains backward compatible and binds new
material to the user's default KB where the product flow creates source items.

Read/list surfaces accept `knowledge_base_id` or repeated/array
`knowledge_base_ids`, including `/workspace/corpus/data`,
`/workspace/documents/data`, `/workspace/graph/data`, `/digest/logs`,
`/workspace/digest/data`, and `/console/reviews/data`.

Security invariant: KB scope is only a candidate-set filter. Retrieval still
intersects KB membership with tenant/user ACL. Inaccessible KB ids fail closed
with `403 {"error": "knowledge base is not accessible"}` and do not return a
partial list of inaccessible ids.

### `POST /workspace/ask`

Primary Ask PSKA endpoint for workspace QA. This is the product-facing route
used by Today, Corpus, and Graph surfaces.

```json
{
  "query": "Agent Runtime",
  "intent": "auto",
  "surface": "today",
  "scope": {
    "mode": "hard",
    "knowledge_base_ids": ["kb_..."],
    "source_item_ids": ["optional-explicit-source-item-id"]
  },
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
either `pska` or `fastreact_pska_mcp`. Deep Ask uses `route.tool_profile="ask_read"`.

Knowledge-base scope contract:

- `scope.knowledge_base_ids` may contain one or more accessible KB ids.
- PSKA resolves KB ids before retrieval, validates access, and compiles active
  KB membership into `source_item_ids`.
- If the request also passes `scope.source_item_ids`, PSKA intersects explicit
  source ids with KB membership.
- `scope.mode="hard"` is implied when KB ids are present and no explicit mode is
  supplied. Hard scope means citations/source refs/source windows must come only
  from resolved `source_item_ids`.
- `route.scope_applied` is the canonical resolved scope. It includes `mode`,
  `knowledge_base_ids`, `knowledge_base_source_item_count`, `source_item_ids`,
  `source_item_count`, `dropped_scope_ids`, `dropped_source_item_ids`,
  `attachment_ids`, `allow_expand_scope`, `conversation_id`, and
  `context_node_count`.
- `dropped_scope_ids` currently records explicit source item ids supplied by the
  caller but removed by KB/source intersection. It is not used to reveal
  inaccessible KB ids.
- `citations`, `source_refs`, `source_windows`, and search `results` include
  `knowledge_base_ids` / `knowledge_base_names` when lineage is known.
- Empty selected KBs surface no-answer diagnostics such as
  `selected_knowledge_base_empty`; explicit source ids outside the selected KB
  surface `selected_scope_empty`.

Deep Ask receives the same resolved scope through `route.tool_policy.scope`:

```json
{
  "mode": "allowlist",
  "allowed_tools": [
    "pska_pska_search",
    "pska_pska_index_status",
    "pska_pska_read_evidence_context",
    "pska_pska_graph_context",
    "pska_pska_digest_context"
  ],
  "scope": {
    "mode": "hard",
    "scope_mode": "hard",
    "knowledge_base_ids": ["kb_..."],
    "source_item_ids": ["src_..."]
  }
}
```

FastReAct must preserve this policy when calling PSKA MCP tools. PSKA MCP also
revalidates KB ids and ACL on every tool call, so tool-policy propagation is a
product consistency requirement, not the only security boundary.

For diagnostics, Deep Ask public traces preserve safe scope audit fields on
FastReAct tool-call events:

- `trace.events[*].tool_args.knowledge_base_ids`
- `trace.events[*].tool_args.source_item_ids`
- `trace.events[*].tool_args.scope_mode`
- `trace.events[*].tool_args.scope`
- `trace.events[*].metadata.tool_policy_scope_applied`
- `trace.events[*].metadata.tool_policy.scope`

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
The `route` event carries the same `scope_applied` and `tool_policy` contract as
the non-streaming response.

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
`surface="writing"`, their own `session_id`, and a structured `scope` containing
directly connected writing nodes/edges.

Core endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /workspace/writing/boards` | List current user's writing boards. |
| `POST /workspace/writing/boards` | Create a board with `title`, `goal`, and optional metadata. |
| `GET /workspace/writing/boards/{board_id}` | Load one board with nodes and edges. |
| `PATCH /workspace/writing/boards/{board_id}` | Update board title, goal, or metadata. |
| `DELETE /workspace/writing/boards/{board_id}` | Delete one board and cascade its nodes and edges within the current tenant/user scope. |
| `POST /workspace/writing/boards/{board_id}/nodes` | Create a `goal`, `question`, `answer`, `evidence`, `gap`, `section`, or `draft` node. |
| `PATCH /workspace/writing/boards/{board_id}/nodes/{node_id}` | Update node title, Markdown body, position, status, citations, source refs, or metadata. |
| `DELETE /workspace/writing/boards/{board_id}/nodes/{node_id}` | Delete a node and its connected edges. |
| `POST /workspace/writing/boards/{board_id}/edges` | Create a typed relation such as `answered_by`, `supported_by`, `raises`, or `included_in`. |
| `DELETE /workspace/writing/boards/{board_id}/edges/{edge_id}` | Delete one relation. |
| `POST /workspace/writing/boards/{board_id}/suggest-questions` | Return generic follow-up question suggestions; does not persist nodes. |
| `POST /workspace/writing/boards/{board_id}/compose` | Build Markdown from selected answer nodes; does not perform retrieval. |
| `POST /workspace/evidence-wiki/publish` | Promote or unpublish an Evidence Brief after review-gate checks. |
| `POST /workspace/evidence-wiki/search` | Search scoped published Evidence Wiki pages. Empty `query` lists published pages in scope. |
| `GET /workspace/evidence-wiki/pages/{board_id}` | Load a read-only published Evidence Wiki page. |
| `POST /workspace/evidence-wiki/pages/{board_id}/taxonomy` | Update durable Evidence Wiki taxonomy metadata for one Evidence Brief page. |

`compose` is intentionally retrieval-free. It only uses the selected answer
nodes' Markdown, citations, and source refs, so the retrieval owner remains the
original Ask PSKA run that produced those answer nodes. Follow-up context comes
from the Inquiry Graph: connected nodes are sent as structured scope, not as a
hidden second search channel.

Published Evidence Wiki page payloads include source refs, lineage, access
hints, review-gate state, `taxonomy`, and `related_pages`. Taxonomy is stored in
Writing board metadata under `wiki_taxonomy` with domain-agnostic `tags`,
`categories`, `topics`, and `collections`; search can filter on these fields
and returns `taxonomy_facets` for the current scoped result set. Related pages
are generated from shared source refs, taxonomy, or shared KB ids among active
published pages in the same tenant/user scope; they are navigation aids, not a
separate retrieval source.

For a complete seed-and-run scenario, see
[`WRITING_WORKSPACE_TEST_CASE.zh.md`](WRITING_WORKSPACE_TEST_CASE.zh.md).

## Review

| Endpoint | Purpose |
| --- | --- |
| `GET /review-items` | Raw review item list |
| `GET /console/reviews/data?status=pending&owner_user_id=user_primary&limit=50` | UI-ready pending review data |
| `POST /review-items/{id}/approve` | Approve a review item |
| `POST /review-items/{id}/reject` | Reject a review item |
| `POST /review-items/{id}/apply` | Apply an approved item |

`/console/reviews/data` returns `review_items` for the selected status plus
`analytics` for the current owner/KB scope, including status counts,
review-type counts, evidence health, apply-readiness counts, and a
`by_review_type` status matrix. Each review item also includes `remediation`
with type-specific blockers and available real Review actions, so product UIs
can explain whether the candidate is ready, blocked, or needs manual judgment.

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

`KnowledgeSource` is the connector/source runtime model, not the user-facing KB
container. Connector state endpoints remain as runtime/adapter support for sync
cursors, manifests, and diagnostics.

## Document Library Product APIs

User-facing product copy may call this corpus surface `资料库`; the API object is
`knowledge_bases`. Reviewed long-term claims, graph, memory, and Evidence
Brief/Writing material remain downstream knowledge artifacts, not replacements
for the KB corpus object. `KnowledgeSource`, `source_item`, `document`, and
`chunk` remain internal/API model names.

| Endpoint | Purpose |
| --- | --- |
| `POST /workspace/sources/upload` | Multipart or JSON upload; creates an upload source item, document, chunks, sync report, optional digest job |
| `POST /workspace/sources/text` | Paste long text or Markdown into the private document library |
| `GET /workspace/documents/data` | List source items/documents with lifecycle state, chunk counts, impact counts, and delete metadata |
| `GET /workspace/reader/source` | Load one active source item's readable source/document/chunk context for Citation Inspector / ReaderPane |
| `POST /workspace/documents/delete` | Preview or execute soft delete, restore, or admin/dev hard purge |
| `POST /workspace/documents/link` | Add existing source items to a target KB without re-ingesting. |
| `POST /workspace/documents/move` | Move source item membership from one KB to another. |

`/workspace/sources/upload` accepts either multipart form fields
(`file`, `filename`, `digest_mode`, `knowledge_base_id`) or JSON
(`bytes_base64`, `text`, `filename`, `knowledge_base_id`).
`digest_mode=after_upload` schedules `digest_via_fastreact`; `manual` only
ingests and indexes. Upload/text sources are private by default and inherit the
request tenant/user from AuthNode/Gateway context.

`/workspace/reader/source` requires `source_item_id`. Optional
`knowledge_base_id` / `knowledge_base_ids` hard-scope the read: if the selected
source is not an active member of the selected KB scope, the endpoint fails
closed. Responses include the source item, KB lineage, documents, ordered
chunks, passage windows, and `scope_applied`.

Document delete defaults to dry-run preview. Soft delete removes documents from
active retrieval and tombstones index state. Reviewed knowledge derived from
removed evidence should be marked stale/reviewable instead of being silently
deleted. Hard purge is for explicit admin/dev cleanup flows.

When `POST /workspace/documents/delete` receives `knowledge_base_id` and
`delete_mode="membership"`, it archives only the KB membership for the selected
source items. The source item remains active in other KBs. `delete_mode="source"`
or `hard_delete=true` affects the source item itself and should be reserved for
explicit delete/purge flows.

## Ask Conversations

| Endpoint | Purpose |
| --- | --- |
| `GET /workspace/ask/conversations` | List active/archived Ask threads |
| `POST /workspace/ask/conversations` | Create a thread |
| `GET /workspace/ask/conversations/{conversation_id}` | Read thread, messages, and runs |
| `POST /workspace/ask/conversations/{conversation_id}/messages/stream` | Stream a multi-turn Ask run and persist user/assistant messages |

The legacy `POST /workspace/ask/stream` remains available. Conversation runs
add recent message context and conversation summary to the Ask scope, but chat
content is not automatically written to long-term knowledge. Users must
explicitly save an answer, citation, or attachment as a writing node, Evidence
Brief draft, or document-library entry.

## Prompt Profiles

| Endpoint | Purpose |
| --- | --- |
| `GET /workspace/prompt-profiles` | List stored tenant/user profiles plus effective merged profiles |
| `GET /workspace/prompt-profiles/effective` | Read only the effective prompt set |
| `PUT /workspace/prompt-profiles` | Upsert tenant or user prompt profiles |

Profile types are `ask`, `digest`, `review`, and `writing`. Effective order is:
system defaults, tenant defaults, user overrides, then single-run overrides.
Artifacts produced by Ask/Digest/Writing should record profile id/version
lineage. Prompt customization cannot bypass source refs, review gates, or
tenant visibility.

## MCP

`POST /mcp` exposes PSKA tools over JSON-RPC for FastReAct or other clients.

Required tools include:

- `pska_search`
- `pska_index_status`
- `pska_read_evidence_context`
- `pska_graph_context`
- `pska_digest_context`
- `pska_job_context`
- `pska_write_candidates`

Ask deep only exposes the `ask_read` profile:

- `pska_pska_search`
- `pska_pska_index_status`
- `pska_pska_read_evidence_context`
- `pska_pska_graph_context`
- `pska_pska_digest_context`

Digest workers use `pska_job_context` and `pska_write_candidates`; admin ingest
and extract tools are not part of Ask. HTTP MCP should receive tenant/user from
AuthNode JWT or trusted headers. Stdio MCP is local/dev only; in that mode
FastReAct is the only security boundary.

Read-only MCP tools accept KB scope either as top-level
`knowledge_base_ids`/`scope_mode` or nested under `scope`. For
`pska_search`, `pska_read_evidence_context`, `pska_graph_context`, and
`pska_digest_context`, KB ids are resolved to source item ids inside PSKA and
then intersected with any explicit `source_item_ids`. If KB ids are present,
PSKA applies hard scope and returns `scope_applied` with `knowledge_base_ids` and
resolved `source_item_ids`. Inaccessible KB ids raise
`knowledge base is not accessible`.
