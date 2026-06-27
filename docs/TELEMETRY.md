# PSKA Telemetry Design

Status: design only. Do not add a new event store until Today and Review are
used with real data.

## Goals

Measure whether the workspace is useful:

- Does the user continue work from Today?
- Do discoveries get accepted, ignored, or postponed?
- Do review items get approved, rejected, or applied?
- Does the user fall back to search after seeing Today?

## Event Names

Initial event vocabulary:

| Event | When |
| --- | --- |
| `workspace_today_viewed` | Today page loads |
| `continue_working_opened` | User opens an item from Continue Working |
| `discovery_accepted` | User accepts a discovery |
| `discovery_ignored` | User ignores a discovery |
| `discovery_snoozed` | User postpones a discovery |
| `review_approved` | User approves a review item |
| `review_rejected` | User rejects a review item |
| `review_applied` | User applies an approved review item |
| `brain_context_refreshed` | User manually refreshes PSKA Brain |
| `workspace_search_executed` | User runs workspace search |
| `workspace_ask_executed` | User runs Ask PSKA |

## Event Shape

```json
{
  "event": "review_approved",
  "surface": "today",
  "user_id": "user_primary",
  "object_type": "review_item",
  "object_id": "rev_xxx",
  "metadata": {
    "review_type": "memory_candidate",
    "source_ref_status": "present"
  },
  "created_at": "2026-06-20T12:00:00Z"
}
```

## V0 Implementation Option

For v0, log structured events through the PSKA HTTP service logger only. Do not
create a permanent analytics table yet.

Possible endpoint:

```text
POST /workspace/events
```

Rules:

- Never log document body text.
- Never log raw prompt or selected text.
- IDs, event names, surface names, counts, route labels, latency, and review
  types are OK.
- Respect existing PSKA service token/auth behavior.

## Ask PSKA Quality Signals

`POST /workspace/ask` returns `quality_signals` and the HTTP service logger
adds the same objective counters to the `pska.http_request` record. This is the
first quality-capture loop for comparing PSKA answers and sidecar RAG answers.

The response and log deliberately avoid raw question text. They include:

- `quality_band`: `grounded`, `no_answerable_evidence`, `needs_review`,
  `needs_citation_review`, or `failed`
- `report_readiness`: `ready_with_citations`, `needs_human_review`,
  `needs_citation_review`, `not_ready`, or `failed`
- `retrieval_owner`, `selected_intent`, `surface`, `fallback_from`
- `citation_count`, `source_ref_count`, `evidence_result_count`,
  `graph_path_count`, `gap_count`, `conflict_count`
- `tool_call_count`, `denied_tool_call_count`
- `query_chars`, `answer_chars`, `total_ms`, `time_to_first_answer_ms`

For stream responses, `quality_signals` appears in the `evidence` and `done`
SSE events. The first visible answer latency is still measured from
`answer_delta`, not route or evidence events.

## Later Storage Option

If event data proves useful, add `workspace_events` with:

- `workspace_event_id`
- `owner_user_id`
- `event_name`
- `surface`
- `object_type`
- `object_id`
- `metadata`
- `created_at`

This should be treated as product telemetry, not canonical knowledge. It should
not affect memory/profile/graph without explicit review logic.
