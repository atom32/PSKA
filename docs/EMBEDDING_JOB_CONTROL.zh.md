# Embedding Job Control

Status: Phase 2 operational slice
Last reviewed: 2026-07-06

PSKA 的 embedding 目标不是“所有资料进入系统后不可控地自动跑完”，而是：

- 可按 tenant/user/KB/source 限定范围。
- 可作为 durable job 排队、重试和审计。
- 可在 KB readiness 中看到 coverage。
- 可在导入高峰时通过 limit、batch size 和 worker 策略控制成本。

## 当前实现

### 同步 ingest

如果运行时 embedding provider 已开启，`IngestService` 和文件同步路径会在
chunk 写入时同步调用 provider 生成 embedding。这能让小批量上传立即可检索，
但不适合大规模导入或受控成本场景。

### Durable backfill job

`embed_backfill` 已是 PSKA durable job type。它现在支持：

- 全库缺失向量 backfill。
- `knowledge_base_id` scope。
- `source_item_ids` scope。
- `tenant_id` / `owner_user_id` 隔离。
- `limit` 和 `batch_size` 控制。
- provider/model 变化后的重嵌入判断。

示例：只处理一个 KB 的缺失 embedding：

`/tmp/pska-embed-backfill.json`:

```json
{
  "tenant_id": "tenant_graphintell",
  "owner_user_id": "test_user_3",
  "knowledge_base_id": "kb_xxx",
  "embedding_provider": "bge-m3",
  "limit": 200,
  "batch_size": 8
}
```

```bash
./scripts/pska --config .pska/config.json job-submit embed_backfill \
  --payload /tmp/pska-embed-backfill.json
```

然后由本地 job worker 消费：

```bash
./scripts/pska --config .pska/config.json job-worker \
  --exclude-job-type digest_via_fastreact \
  --max-jobs 1
```

`digest_via_fastreact` 仍由 FastReAct worker 消费；embedding backfill 属于
PSKA 本地后台任务，不进入 FastReAct agentic loop。

## 可观测性

已有：

- KB readiness: `embedding_coverage`, `embedding_status`, `embedding_models`。
- job stats: `embedding_backlog.jobs`, `embedding_backlog.source_items`,
  `embedding_backlog.knowledge_bases`。
- job detail/events: durable job result 会记录 embedded/skipped/failed/errors
  和 scope。

仍需产品化：

- KB 处理页上的“开始/暂停/重试 embedding”按钮。
- 每个 KB 的 queued/running embedding job 列表。
- 大规模导入时的默认异步 embedding 模式。
- embedding provider readiness smoke，例如 BGE-M3 是否可加载、vector search
  是否能返回候选。

## Release Gate

试点前建议把 embedding 定义为可控后台处理：

1. 大批量导入默认不在 HTTP 请求内同步跑完整 embedding。
2. KB readiness 能显示缺失 chunk 数和后台 job 状态。
3. 用户或管理员可以按 KB 触发 backfill。
4. worker 可通过 `limit` / `batch_size` 控制吞吐。
5. 失败不会静默降级，必须进入 job result 或 readiness warning。
