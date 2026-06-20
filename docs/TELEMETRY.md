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
- IDs, event names, surface names, counts, and review types are OK.
- Respect existing PSKA service token/auth behavior.

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
