# PSKA Operations Runbook

日期：2026-06-14

状态：P0.5 foreground service runbook。PSKA 仍不是 daemon supervisor；自动启动、重启策略、日志轮转、launchd/systemd 配置留给后续部署阶段。

## 1. Environment

推荐本地配置文件：

```json
{
  "database": {"url": "postgresql:///pska"},
  "service": {"host": "127.0.0.1", "port": 8765},
  "llm": {"api_key_file": "~/api_key.txt", "timeout_seconds": 60},
  "fastreact": {"url": "http://127.0.0.1:8000", "timeout_seconds": 30},
  "embedding": {"provider": "disabled"}
}
```

默认路径为 `~/.pska/config.json`、当前目录 `.pska/config.json` 或 `config.pska.json`。环境变量仍可覆盖 config file：

```bash
export PSKA_DATABASE_URL=postgresql:///pska
export PSKA_SERVICE_URL=http://127.0.0.1:8765
export PSKA_FASTREACT_URL=http://127.0.0.1:8000

# P0.2 起支持。设置后 /health 以外的 HTTP route 都需要 token。
export PSKA_SERVICE_TOKEN=replace-with-local-token

# Optional。
export PSKA_FASTREACT_SERVICE_TOKEN=replace-with-fastreact-token
export PSKA_EMBEDDING_PROVIDER=disabled
```

## 2. Database

检查 PostgreSQL 和 pgvector：

```bash
./scripts/pska db-check
```

初始化或补齐 migrations：

```bash
./scripts/pska db-init
```

## 3. Start PSKA Online Service

前台启动 HTTP service：

```bash
./scripts/pska --config .pska/config.json serve --host 127.0.0.1 --port 8765
```

另开一个终端做 contract smoke：

```bash
./scripts/pska service-check --url http://127.0.0.1:8765
```

如果启用了 `PSKA_SERVICE_TOKEN`：

```bash
./scripts/pska service-check \
  --url http://127.0.0.1:8765 \
  --service-token "$PSKA_SERVICE_TOKEN"
```

`service-check` validates:

- `GET /health`
- `GET /ready`
- `POST /mcp` with `tools/list`
- MCP tools include `pska_search`

Fastreact offline does not make PSKA unavailable. In that case `/ready` should still have `ok=true`, with `checks.fastreact.ok=false`.

## 4. Start Worker

前台启动 durable job worker：

```bash
./scripts/pska job-worker \
  --worker-id pska-worker-local \
  --lease-seconds 300 \
  --poll-interval 5 \
  --recover-stale-seconds 900
```

单次处理 queued jobs：

```bash
./scripts/pska job-run \
  --worker-id pska-worker-local \
  --lease-seconds 300 \
  --limit 1
```

## 5. Jobs

提交 Fastreact-backed extraction job：

```bash
./scripts/pska job-submit extract_via_fastreact \
  --payload ./job-payload.json \
  --max-attempts 3
```

查看 job backlog：

```bash
./scripts/pska job-status --limit 20
```

查看某个 job 和 events：

```bash
./scripts/pska job-status --job-id job_xxx
```

`queued` job 可能因为 retry backoff 暂时不可领取；查看 job 的 `run_after`。digest worker 可用 `GET /digest/batches/{job_id}?cursor=0&limit=20` 分页读取上下文，直到 `has_more=false`。

恢复 stale running jobs：

```bash
./scripts/pska job-recover --max-age-seconds 900
```

重试 failed job：

```bash
./scripts/pska job-retry job_xxx
```

## 6. Readiness Interpretation

Local PSKA availability:

- `checks.database.ok=true`
- `checks.schema.ok=true`
- `checks.mcp.ok=true`

Operational signals:

- `checks.jobs.running_stale_count > 0` means worker lease expired; run `job-recover`.
- `checks.jobs.recent_failed` shows recent failed jobs and `external_run_id` when Fastreact was involved.
- `checks.fastreact.ok=false` means Fastreact is offline or not ready; PSKA can still do local retrieval and manage backlog.
- `checks.fastreact.pska_tools_loaded=false` means Fastreact is reachable but missing required PSKA tools.
- `checks.mcp.missing_required_tools` means PSKA's local MCP contract is broken.

## 7. Fastreact Boundary

目标形态是 Fastreact 通过 HTTP MCP 调用 PSKA：

```text
endpoint: http://127.0.0.1:8765/mcp
transport: http
```

当前实测稳定路径是 HTTP MCP。生成并启动 HTTP MCP 配置：

```bash
./scripts/fastreact-pska-service-config \
  --mcp-transport http \
  --output .pska/fastreact-pska-http.json

# Fastreact credentials must include mcp_api_keys.pska.
# Store PSKA_SERVICE_TOKEN there; do not commit credentials.json.

cd ~/Fastreact/fastreact-nano
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src \
  python3 -m fastreact.adapters.http \
  --config "/Users/xudawei/Documents/personal archive/.pska/fastreact-pska-http.json"
```

验证：

```bash
./scripts/pska --config .pska/config.json service-check
```

PSKA owns storage, ACL, source refs, review, audit, jobs, citations, and MCP/API. Fastreact owns LLM calls, planning, tool orchestration, run lifecycle, SSE events, approval, and trace.
