# PSKA Online Service Contract

日期：2026-06-14

状态：本地 online service contract + service auth/request context + durable job worker metadata + readiness observability + foreground service runbook + Fastreact candidate write-back + digest schedule API/CLI + Knowledge Source/files sync + connector runtime API + config file support。

PSKA online service 是前台运行的本地 HTTP 服务。它不是 daemon supervisor；自动启动、重启、worker supervision 和运维手册属于 P0.5。

命令示例使用占位变量表示本机目录：

```bash
export PSKA_REPO="/path/to/pska"
export NOTES_ROOT="/path/to/notes"
export FASTREACT_NANO_REPO="/path/to/FastReAct/fastreact-nano"
```

启动：

```bash
./scripts/pska --config .pska/config.json serve --port 8765
```

在线 contract smoke：

```bash
./scripts/pska service-check --url http://127.0.0.1:8765
```

运维手册见 [operations-runbook-zh.md](./operations-runbook-zh.md)。

默认地址：

```text
http://127.0.0.1:8765
```

## Configuration

PSKA 兼容原来的环境变量方式，也支持 JSON config file。加载顺序：

- 显式 `./scripts/pska --config /path/to/config.json ...`
- `~/.pska/config.json`
- 当前目录 `.pska/config.json`
- 当前目录 `config.pska.json`

环境变量优先级高于 config file。推荐本地配置：

```json
{
  "database": {"url": "postgresql:///pska"},
  "service": {"host": "127.0.0.1", "port": 8765},
  "llm": {"api_key_file": "~/api_key.txt", "timeout_seconds": 60},
  "fastreact": {"url": "http://127.0.0.1:18741", "timeout_seconds": 30},
  "embedding": {"provider": "disabled"}
}
```

`~/api_key.txt` 可提供 LLM API key、model、base URL，以及 PSKA/Fastreact service token。PSKA 不会在 readiness 或 CLI 输出中打印密钥值。

## HTTP Endpoints

Readiness and status：

- `GET /health`
- `GET /ready`
- `GET /index-status`
- `GET /metrics`

Knowledge：

- `POST /search`
- `POST /agentic-search`
- `POST /ingest/channel-payload`
- `POST /connectors/records`
- `GET /connectors/states`
- `GET /connectors/states/{connector_state_id}`
- `POST /connectors/states`
- `POST /connectors/states/{connector_state_id}`
- `POST /candidates`

Jobs：

- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/context`
- `POST /jobs/{job_id}/lease`
- `POST /jobs/{job_id}/complete`
- `POST /jobs/{job_id}/fail`
- `POST /jobs/run`
- `POST /jobs/recover`
- `POST /jobs/{job_id}/retry`

Digest：

- `POST /digest/schedule`
- `GET /digest/batches/{job_id}`
- `POST /digest/candidates`

Review：

- `GET /review-items`
- `POST /review-items/{review_item_id}/approve`
- `POST /review-items/{review_item_id}/reject`
- `POST /review-items/{review_item_id}/apply`

MCP：

- `POST /mcp`

## Authentication and Request Context

默认本地开发不强制认证。在 `.pska/config.json` 设置 `service.service_token` 后，除 `GET /health` 外的所有 HTTP route 都需要 service token：

```json
"service": {
  "host": "127.0.0.1",
  "port": 8765,
  "service_token": "replace-with-local-token"
}
```

支持两种 header：

```http
X-PSKA-Service-Token: replace-with-local-token
Authorization: Bearer replace-with-local-token
```

请求身份可通过 header 或 JSON payload 传入：

```http
X-PSKA-Caller: user|agent_service|service
X-PSKA-User-Id: user_primary
X-PSKA-Represented-User-Id: user_primary
X-PSKA-Scope: {"source_item_ids":["..."]}
```

规则：

- 未提供身份时默认 `user_id=user_primary`、`caller=user`。
- `caller=agent_service` 时，PSKA 强制以 `agent_service` 作为 request user。
- `agent_service` 没有 `represented_user_id` 时不能读取私有知识。
- `represented_user_id` 只能让 agent 读取该用户本来可见的知识；ACL 决策仍由 PSKA 执行。
- Fastreact 或其他 agentic layer 不能直接访问 PSKA DB，也不能绕过 PSKA MCP/API ACL。

## `/ready`

`GET /ready` returns a dependency report:

```json
{
  "ok": true,
  "checks": {
    "database": {"ok": true},
    "schema": {"ok": true, "tables": {}, "missing": []},
    "index": {"ok": true, "counts": {}},
    "embedding": {"provider": "disabled", "configured": false},
    "llm": {"api_key_file_configured": false},
    "jobs": {
      "ok": true,
      "sample_size": 0,
      "by_status": {},
      "by_type": {},
      "active_worker_ids": [],
      "running_stale_count": 0,
      "stale_running": [],
      "recent_failed": []
    },
    "metrics": {
      "ok": true,
      "embedding": {
        "provider": "disabled",
        "model": "BAAI/bge-m3",
        "total_chunks": 0,
        "embedded_chunks": 0,
        "missing_chunks": 0,
        "coverage": 1.0
      },
      "connectors": {
        "source_channel_count": 0,
        "source_channels": {}
      }
    },
    "fastreact": {
      "ok": false,
      "url": "http://127.0.0.1:18741",
      "pska_tools_loaded": false,
      "missing_pska_tools": []
    },
    "mcp": {
      "ok": true,
      "protocol_version": "2024-11-05",
      "tool_count": 10,
      "tools": ["pska_search"],
      "required_tools": [
        "pska_search",
        "pska_index_status",
        "pska_read_evidence_context",
        "pska_graph_context",
        "pska_digest_context",
        "pska_job_context",
        "pska_write_candidates"
      ],
      "missing_required_tools": []
    }
  }
}
```

`ok=true` means PSKA's local DB/schema/MCP contract is usable. Fastreact may be offline; that sets `checks.fastreact.ok=false` but does not make PSKA itself unavailable.

P0.4 readiness observability notes：

- `checks.jobs.running_stale_count` counts running jobs whose lease has expired.
- `checks.jobs.digest_backlog` reports queued/running digest jobs and scoped source count.
- `checks.metrics.embedding` reports current provider/model coverage plus any-provider coverage.
- `checks.metrics.connectors` reports source-channel freshness plus adapter/runtime state. User-facing source health should be shown as Knowledge Sources and sync reports.
- `checks.jobs.recent_failed` includes a bounded list of recent failed jobs with error and external run reference.
- `checks.fastreact.pska_tools_loaded=false` means Fastreact is reachable but does not expose all PSKA required tools.
- `checks.mcp.missing_required_tools` is a local contract failure and makes `ok=false` if required PSKA MCP tools are missing.

## Request IDs and Logs

HTTP clients may pass either header:

```http
X-PSKA-Request-Id: req_xxx
X-Request-Id: req_xxx
```

PSKA returns the selected id in every HTTP response:

```http
X-PSKA-Request-Id: req_xxx
```

The foreground service writes one structured JSON log line per HTTP response to stderr:

```json
{
  "event": "pska.http_request",
  "request_id": "req_xxx",
  "method": "POST",
  "path": "/jobs/job_xxx/lease",
  "status": 200,
  "duration_ms": 2.4,
  "caller": "agent_service",
  "user_id": "agent_service",
  "represented_user_id": "user_primary",
  "job_id": "job_xxx",
  "source_item_ids_count": 3
}
```

Logs intentionally avoid request bodies, content text, tokens, and generated knowledge payloads. They are for correlation across PSKA, Fastreact, and worker logs.

## Connector Record Contract

P1 connector implementations should emit `pska.connector_record.v1` records and submit them to:

```http
POST /connectors/records
```

Minimal shape:

```json
{
  "schema_version": "pska.connector_record.v1",
  "connector_id": "files",
  "external_id": "/path/to/notes/project.md",
  "source_uri": "file:///path/to/notes/project.md",
  "record_type": "file",
  "title": "Project note",
  "body": "Readable text extracted by the connector.",
  "owner_user_id": "user_primary",
  "space_id": "private_primary",
  "visibility": "private",
  "visible_team_ids": [],
  "created_at": "2026-06-16T10:00:00Z",
  "updated_at": "2026-06-16T10:05:00Z",
  "captured_at": "2026-06-16T10:06:00Z",
  "artifacts": {"path": "/path/to/notes/project.md"},
  "permission_metadata": {"root_id": "notes", "read_scope": "explicit_directory"},
  "scan_cursor": "cursor_xxx",
  "content_hash": "sha256:..."
}
```

PSKA converts this to `pska.channel_ingest.v1` and stores it as normal `source_items`, `documents`, and `chunks`. Connector metadata is preserved under `source_items.metadata.extra.connector`, and permission metadata is preserved under `source_items.metadata.extra.permission_metadata`.

Rules:

- `connector_id` becomes `source_channel`.
- `external_id` becomes `source_id`.
- `source_uri` becomes `url`.
- `artifacts` becomes `raw_paths`.
- `body` is the canonical readable text.
- `visibility`, `visible_team_ids`, `owner_user_id`, and `space_id` remain PSKA-owned ACL fields.
- `scan_cursor` is preserved on the source item and can also be persisted in adapter runtime state.

Local CLI:

```bash
./scripts/pska connector-ingest-record ./record.json
```

## Connector Runtime State Contract

Knowledge Source is the user-facing lifecycle object. Connector state is an
implementation/runtime detail for adapters that need enablement, authorization
scope, lightweight manifests, and incremental scan cursors.

Adapter implementations can persist runtime state through `pska.connector_state.v1`:

```http
POST /connectors/states
GET /connectors/states?owner_user_id=user_primary&connector_id=files
GET /connectors/states/conn_user_primary_files
```

Example:

```json
{
  "schema_version": "pska.connector_state.v1",
  "connector_id": "files",
  "owner_user_id": "user_primary",
  "enabled": true,
  "scan_cursor": "cursor_xxx",
  "sync_status": "succeeded",
  "permission_scope": {"roots": ["/path/to/notes"]},
  "config": {"ignore": ["*.tmp", ".git/**"]}
}
```

Local CLI:

```bash
./scripts/pska connector-state upsert \
  --connector-id files \
  --owner-user-id user_primary \
  --scan-cursor cursor_xxx \
  --permission-scope-json '{"roots":["/path/to/notes"]}'

./scripts/pska connector-state list --owner-user-id user_primary
./scripts/pska connector-state show conn_user_primary_files
```

## Files / Source Sync

Current files/source sync:

```bash
./scripts/pska files-sync
./scripts/pska files-watch --initial-sync

./scripts/pska files-scan \
  --root "$NOTES_ROOT" \
  --owner-user-id user_primary \
  --ignore '*.tmp'
```

`files-sync` reads active folder Knowledge Sources plus config defaults/seeds.
It also imports the workspace Twitter/X archive inbox. `files-watch` uses
optional `watchdog` support from `pska-core[watch]` to run the same sync path
whenever authorized roots change. `files-scan` is the explicit one-off form.
The current slice supports UTF-8 text-like files such as Markdown, text, JSON,
YAML, CSV/TSV, logs, and Python files. With optional `pska-core[documents]`, it
also uses mature extractors `pypdf` and `python-docx` for PDF/DOCX text,
the built-in XLSX parser for workbook tables, and optional `xlrd` for legacy XLS
extraction. The built-in PDF path is text-only; PDF table reconstruction, scanned
PDFs, and parser-only image files use the optional `document_parser` bridge to an
external service such as `~/DocParserServer` at
`/rag/model_parser_file`. UI uploads use the same extraction path;
`files.max_bytes`, `files.spreadsheet_max_rows_per_sheet`,
`files.spreadsheet_max_columns`, and `document_parser.*` are runtime config
knobs for upload/folder extraction. Sync records file path, file URI, mime type, size, scan cursor,
content hash, authorized root, and lightweight manifests so reports can
distinguish new, changed, unchanged, moved, and missing files without deleting
canonical source history. Richer source versioning UI and Knowledge Sources
file/folder management remain later product work.

## Jobs and Workers

PSKA job system 是 durable orchestrator，不是复杂 agent loop 执行层。P0.3 之后，job 会记录 worker 与外部 run metadata：

Operational job endpoints:

- `GET /jobs?status=&job_type=&limit=` lists jobs with filters.
- `GET /jobs/stats` returns status/type counts, active workers, stale running jobs, and recent failures.
- `POST /jobs/{job_id}/cancel` cancels a queued/running job.
- `POST /jobs/{job_id}/retry` requeues a failed/canceled job.
- `POST /jobs/recover-stale` requeues or fails stale running jobs based on attempts.

- `worker_id`：领取该 job 的 PSKA worker。
- `leased_until`：当前 worker lease 到期时间；job 成功、失败、重试或 stale recovery 时清空。
- `heartbeat_at`：worker 最近一次 heartbeat 时间。
- `external_run_id`：Fastreact 返回的 `run_id`，用于把 PSKA job 与 Fastreact trace/run 对齐。
- `source_refs`：job payload 中声明的 source refs，用于追踪任务输入范围。

CLI 启动方式：

```bash
./scripts/pska job-run --limit 1 --worker-id pska-worker-local --lease-seconds 300
./scripts/pska job-worker --poll-interval 5 --worker-id pska-worker-local --lease-seconds 300
```

Fastreact-backed jobs such as `extract_via_fastreact` and `digest_via_fastreact` submit work to Fastreact, store the returned `run_id` as `external_run_id`, emit `fastreact_submitted` and `heartbeat` job events, then succeed/fail according to the Fastreact client result. PSKA does not copy Fastreact internal trace objects.

## Fastreact Candidate Write-Back

P0.4 write-back slice adds:

- `POST /digest/schedule`
- `GET /jobs/{job_id}/context`
- `POST /jobs/{job_id}/lease`
- `POST /jobs/{job_id}/complete`
- `POST /jobs/{job_id}/fail`
- `GET /digest/batches/{job_id}`
- `POST /candidates`
- `POST /digest/candidates`
- MCP `pska_job_context`
- MCP `pska_write_candidates`
- job `priority`
- retry `run_after` backoff
- digest batch cursor
- candidate `schema_version`

`POST /digest/schedule` creates a `digest_via_fastreact` job from source backlog:

```json
{
  "owner_user_id": "user_primary",
  "source_item_ids": ["src_xxx"],
  "limit": 20,
  "batch_size": 20,
  "priority": 0,
  "max_attempts": 3,
  "retry_backoff_seconds": 60,
  "quota_window_seconds": 86400,
  "max_jobs_per_window": 24,
  "force": false
}
```

It skips source items already covered by any existing digest job unless `force=true`. This includes queued, running, succeeded, failed, and canceled jobs, so automatic digest does not repeat failed work forever. `quota_window_seconds` and `max_jobs_per_window` optionally cap automatic job creation for an owner; when the quota is hit the response has `quota_limited=true` and no job is created. Manual redigest should pass `force=true` and, preferably, an explicit `source_item_ids` scope. The response includes the created job, `scheduled_source_item_ids`, `skipped_source_item_ids`, and `quota`.

For local operation, CLI `digest-scheduler` provides a foreground periodic loop over this endpoint:

```bash
./scripts/pska digest-scheduler --interval-seconds 300 --max-backlog-jobs 10
```

This is not a daemon supervisor. It creates digest backlog only; Fastreact still owns the digest worker loop and LLM execution.

`pska_job_context` returns the job, scoped source items, and chunks from the job's `source_refs`, `source_item_ids`, or `payload.scope.source_item_ids`. It is read-only and filtered to the request or represented user.

`pska_write_candidates` and `POST /candidates` accept grounded candidates:

```json
{
  "owner_user_id": "user_primary",
  "job_id": "job_xxx",
  "request_id": "run_xxx",
  "producer": "fastreact",
  "source_refs": [{"source_item_id": "src_xxx"}],
  "entities": [
    {"entity_type": "project", "label": "PSKA", "confidence": 0.9}
  ],
  "hyperedges": [
    {
      "relation_type": "depends_on",
      "directionality": "directed",
      "evidence_text": "PSKA uses Fastreact for agentic loops.",
      "confidence": 0.85,
      "members": [
        {"entity_type": "project", "label": "PSKA", "role": "system"},
        {"entity_type": "service", "label": "Fastreact", "role": "executor"}
      ]
    }
  ],
  "review_items": [
    {"review_type": "conflict", "title": "Check claim", "proposal": {"note": "Needs review"}}
  ],
  "memory_candidates": [
    {"kind": "agent_memory", "layer": "semantic", "text": "PSKA keeps Postgres as source of truth.", "confidence": 0.8}
  ]
}
```

Rules:

- `source_refs` are required and must reference known `source_items`.
- referenced source items must belong to `owner_user_id`.
- supported `review_type` values are `share_proposal`, `sensitive_content`, `profile_update`, `entity_merge`, `conflict`, `memory_candidate`, `relationship_candidate`, `action_candidate`, and `low_confidence`.
- high/sensitive profile or memory candidates go through review.
- low-confidence memory and relationship candidates go through review instead of being written directly to memory/graph.
- writes emit an audit event and return a summary of created entities, hyperedges, review items, memories, and profile cards.

External worker lifecycle:

```bash
curl http://127.0.0.1:8765/jobs/job_xxx/lease \
  -H 'Content-Type: application/json' \
  -d '{"worker_id":"fastreact-worker","lease_seconds":300}'
```

Lease response includes:

- leased `job`
- scoped `context`
- `allowed_tools`
- `lease_seconds`

For digest workers, `GET /digest/batches/{job_id}` returns a paged job context. It accepts:

```text
cursor=0
limit=20
```

The response includes `next_cursor`, `has_more`, `batch_size`, and `total_source_items`. `POST /digest/candidates` is currently an alias for `POST /candidates`.

Complete/fail:

```bash
curl http://127.0.0.1:8765/jobs/job_xxx/complete \
  -H 'Content-Type: application/json' \
  -d '{"result":{"ok":true}}'

curl http://127.0.0.1:8765/jobs/job_xxx/fail \
  -H 'Content-Type: application/json' \
  -d '{"error":"Fastreact failed","retryable":true}'
```

Retryable failures requeue the job with exponential backoff. The base delay is `payload.retry_backoff_seconds` or `payload.backoff_seconds`, defaulting to 60 seconds and capped at 3600 seconds. `POST /jobs/{job_id}/retry` resets `run_after` to now.

## HTTP MCP

`POST /mcp` accepts one JSON-RPC object per request and reuses the same `MCPServer.handle()` implementation as stdio MCP.

Initialize:

```bash
curl http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

List tools:

```bash
curl http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Call search:

```bash
curl http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'X-PSKA-Caller: agent_service' \
  -H 'X-PSKA-Represented-User-Id: user_primary' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "pska_search",
      "arguments": {"query": "Project Atlas", "user_id": "user_primary"}
    }
  }'
```

`notifications/initialized` returns HTTP `204 No Content`.

Unknown methods or tool errors return JSON-RPC error objects.

## Fastreact MCP Configuration

目标 contract 是 Fastreact 通过 HTTP MCP 消费 PSKA：

```json
{
  "mcp": {
    "servers": [
      {
        "name": "pska",
        "transport": "http",
        "url": "http://127.0.0.1:8765/mcp",
        "identity_forwarding": {
          "mode": "authnode_jwt",
          "audience": "pska"
        }
      }
    ]
  }
}
```

当前实测状态（2026-06-15）：Fastreact HTTP MCP loader 已可把 `transport: "http"`、`url`、`auth_token_ref` 传入 MCP manager。推荐联调路径为 HTTP MCP：

```bash
./scripts/fastreact-pska-service-config \
  --mcp-transport http \
  --output .pska/fastreact-pska-http.json

# Fastreact credentials must contain mcp_api_keys.pska with the PSKA service.service_token value.
# Example shape, do not commit real tokens:
# ~/.fastreact/credentials.json
# {"mcp_api_keys":{"pska":"..."}}

cd "$FASTREACT_NANO_REPO"
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src \
  python3 -m fastreact.adapters.http \
  --config "$PSKA_REPO/.pska/fastreact-pska-http.json"
```

PSKA `/ready` accepts Fastreact namespaced MCP tools such as `pska_pska_search` and normalizes them when checking `checks.fastreact.pska_tools_loaded`.

生成的 PSKA Fastreact profile 不再依赖 `tenant_rules.pska`。PSKA-facing daemon
使用全局 `tool_rules` deny `exec`、`read_file`、`write_file`、`edit_file`，
并用 `tool_profiles.pska_ask_read` 表达 Ask deep 只读工具集：
`pska_pska_search`、`pska_pska_index_status`、`pska_pska_read_evidence_context`、
`pska_pska_graph_context`、`pska_pska_digest_context`。digest worker 另用
`pska_pska_job_context` 和 `pska_pska_write_candidates`；admin ingest/extract/review
不进入普通 Ask。

Fastreact still must not access PSKA DB directly. PSKA remains the authority for ACL, source refs, review, audit, and persistence.

## Verified Interop

2026-06-15 本地真实 LLM 联调已验证：

- PSKA `GET /ready` reports `database/schema/mcp/fastreact` ok.
- Fastreact HTTP MCP config reports PSKA MCP server alive and exposes `pska_pska_search`, `pska_pska_job_context`, `pska_pska_write_candidates` 等工具。
- PSKA can ingest a canary source via `POST /ingest/channel-payload`.
- Fastreact `/v1/chat/completions` can call PSKA MCP search over HTTP MCP and return the PSKA citation IDs.
- Fastreact `/v1/runs` durable path emits `tool_call -> tool_result -> session_end` events with `tool_name=pska_pska_search`.

Known gaps:

- `/v1/chat/completions` returns a `run_id` but that id is not always available through Fastreact `/v1/runs/{run_id}`.
- `/v1/runs` currently reports terminal status as `completed`; PSKA job completion still uses `chat_completion` for Fastreact-backed jobs.
