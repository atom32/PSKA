# PSKA Operations Runbook

日期：2026-06-14

状态：P0.5 local daemon runbook。PSKA 已有 foreground `local-daemon` supervisor；自动登录启动、重启策略、日志轮转、launchd/systemd 安装仍留给后续部署阶段。

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

当前产品化约定：

- PSKA canonical database 是 `.pska/config.json` 里的 `database.url`，当前为 `postgresql:///pska`。
- 临时样例库例如 `postgresql:///pska_mvp_plus_sample` 只用于 gate/实验，不应作为 daemon 默认库。
- Fastreact 没有 PSKA 知识数据库；它只能通过 PSKA HTTP API/MCP 访问 canonical DB。
- `service-check` 会比较 `/health.database` 和 config 的 `database.url`，发现服务指到错库时会失败。

## 2. Database

检查 PostgreSQL 和 pgvector：

```bash
./scripts/pska db-check
```

初始化或补齐 migrations：

```bash
./scripts/pska db-init
```

## 3. MVP Bootstrap And Status

MVP 推荐先用有限数据源启动：

```bash
./scripts/pska mvp-bootstrap \
  --twitter-archive ~/Downloads/twitter_archive \
  --notes-root ~/Documents/notes \
  --extract
```

这个命令会：

- 执行非破坏性的 `db-init`，不会 reset 数据库。
- 如果 Twitter/X archive 目录存在，则导入 zip。
- 如果传入 `--notes-root`，则扫描本地文本类文件。
- 创建 digest backlog。
- 如果传入 `--extract`，则在有限数据集上构建第一版 entities/hyperedges。
- 输出当前 `mvp-status` 和 next actions。

查看当前 MVP 状态：

```bash
./scripts/pska mvp-status
./scripts/pska mvp-status --summary
```

`mvp-status` 会报告 readiness、index/connector/job metrics、pending review 数量和下一步动作。日常巡检优先使用 `--summary`，输出更紧凑，适合判断 PSKA 是否进入可用状态。

当前 Postgres 样例库 gate：

```bash
cd core
PYTHONPATH=src ../.pska/venvs/pska-py312/bin/python scripts/current_sample_gate.py \
  --database-url postgresql:///pska \
  --require-graph \
  --require-review-or-memory
```

这个 gate 不 reset、不导入、不写入，只读取当前库，验证 source/chunk/search/citation/digest job/graph grounding/review-or-memory 是否已经形成 MVP+ 闭环。

待审候选处理：

```bash
./scripts/pska review-list --status pending --summary
./scripts/pska review-approve <review_item_id> --reason "looks right"
./scripts/pska review-reject <review_item_id> --reason "not grounded enough"
```

## 4. Start PSKA Local Daemon

MVP 推荐用一个前台 supervisor 启动 PSKA service、job worker 和 digest scheduler：

```bash
./scripts/pska --config .pska/config.json local-daemon
```

它会启动：

- `serve`
- `job-worker`
- `digest-scheduler`

可选：

```bash
./scripts/pska local-daemon --restart
./scripts/pska local-daemon --no-worker
./scripts/pska local-daemon --no-digest-scheduler
```

这是本地前台 daemon，适合终端、tmux 或后续 launchd/systemd wrapper。Fastreact service 和 Fastreact digest worker 仍由 Fastreact 项目负责启动。

## 5. Start PSKA Online Service Manually

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

## 6. Start Worker Manually

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

## 7. Jobs

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
./scripts/pska jobs list --status queued --job-type digest_via_fastreact
./scripts/pska jobs stats
./scripts/pska jobs show job_xxx
```

从 source backlog 创建 digest job：

```bash
./scripts/pska digest-schedule \
  --owner-user-id user_primary \
  --limit 20 \
  --batch-size 20 \
  --priority 0
```

`digest-schedule` 会跳过已经被 queued/running/succeeded digest job 覆盖的 source item；需要重做时加 `--force`，或用 `--source-item-id src_xxx` 指定范围。

前台周期性创建 digest backlog：

```bash
./scripts/pska digest-scheduler \
  --owner-user-id user_primary \
  --interval-seconds 300 \
  --limit 20 \
  --batch-size 20 \
  --max-backlog-jobs 10 \
  --recover-stale-seconds 900
```

这是一个 foreground loop，适合本地终端、tmux 或后续 launchd/systemd wrapper。它不会执行 LLM digest；Fastreact 侧 digest worker 仍负责 lease job、调用模型和写回 candidates。

`queued` job 可能因为 retry backoff 暂时不可领取；查看 job 的 `run_after`。digest worker 可用 `GET /digest/batches/{job_id}?cursor=0&limit=20` 分页读取上下文，直到 `has_more=false`。

Fastreact 侧脚本型 digest worker：

```bash
cd ~/Fastreact/fastreact-nano
python3 scripts/pska_digest_worker.py \
  --pska-url http://127.0.0.1:8765 \
  --fastreact-url http://127.0.0.1:8000 \
  --batch-limit 20
```

该 worker 只通过 PSKA HTTP API/MCP 获取上下文和写回 candidates，不直接访问 PSKA DB。

恢复 stale running jobs：

```bash
./scripts/pska job-recover --max-age-seconds 900
```

重试 failed job：

```bash
./scripts/pska job-retry job_xxx
```

取消 queued/running job：

```bash
./scripts/pska job-cancel job_xxx --reason "superseded"
```

## 8. Readiness Interpretation

Local PSKA availability:

- `checks.database.ok=true`
- `checks.schema.ok=true`
- `checks.mcp.ok=true`

Operational signals:

- `checks.jobs.running_stale_count > 0` means worker lease expired; run `job-recover`.
- `checks.jobs.digest_backlog.jobs > 0` means there are queued/running digest jobs for Fastreact workers.
- `checks.metrics.embedding.coverage < 1` means some chunks are missing embeddings for the currently configured provider/model; run `embed-backfill` when semantic retrieval is enabled.
- `checks.metrics.connectors.source_channels` shows last source-item freshness by channel.
- `checks.metrics.connectors.state_count` and `state_sync_status` show durable connector state readiness.
- `checks.jobs.recent_failed` shows recent failed jobs and `external_run_id` when Fastreact was involved.
- `checks.fastreact.ok=false` means Fastreact is offline or not ready; PSKA can still do local retrieval and manage backlog.
- `checks.fastreact.pska_tools_loaded=false` means Fastreact is reachable but missing required PSKA tools.
- `checks.mcp.missing_required_tools` means PSKA's local MCP contract is broken.

HTTP service logs:

- Every HTTP response emits one JSON line to stderr with `event=pska.http_request`.
- Pass `X-PSKA-Request-Id` or `X-Request-Id` from clients/workers to correlate PSKA logs with Fastreact traces.
- Logs include request id, method, path, status, duration, caller/user, represented user, job id, and source ref count.
- Logs intentionally omit request bodies, content text, tokens, and candidate payloads.

## 9. Fastreact Boundary

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

如果 Fastreact 使用 stdio MCP，生成配置时也必须使用同一个 `--database-url postgresql:///pska`。如果 Fastreact 使用 HTTP MCP，则它访问的是 `http://127.0.0.1:8765/mcp` 背后的 PSKA service，因此 PSKA service 自己必须通过 `service-check` 的 database alignment。

PSKA owns storage, ACL, source refs, review, audit, jobs, citations, and MCP/API. Fastreact owns LLM calls, planning, tool orchestration, run lifecycle, SSE events, approval, and trace.
