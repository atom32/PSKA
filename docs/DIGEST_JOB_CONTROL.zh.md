# Digest Job Control

Status: Phase 2 operational slice
Last reviewed: 2026-07-06

Digest 是会消耗 FastReAct/LLM 吞吐的后台能力，不能像普通索引任务一样无节制排队。
PSKA 的设计边界是：

- PSKA scheduler 只负责发现需要 digest 的资料并排 `digest_via_fastreact` job。
- PSKA local job worker 不消费 `digest_via_fastreact`。
- FastReAct digest worker 才负责 LLM/tool reasoning 和候选写回。
- Digest 任务必须可限频、可限量、可观测、可手动暂停。

## 当前默认策略

`./start.sh` 会启动 `local-daemon`，但默认不包含 digest scheduler。也就是说：

```text
periodic_digest_scheduler = disabled
manual_digest_schedule = available
manual_digest_now = available
```

含义：

- 不会因为启动系统而自动消耗 FastReAct/LLM 吞吐。
- 上传、同步和普通 Ask 不依赖 digest 内容。
- 需要 digest 时，用户或运维显式运行 `digest-schedule` 或 `digest-now`。

如果确实需要定时 digest，需要显式开启：

```bash
./scripts/pska --config .pska/config.json local-daemon \
  --digest-scheduler \
  --digest-interval-seconds 300 \
  --digest-limit 20 \
  --digest-batch-size 1 \
  --digest-max-backlog-jobs 10 \
  --digest-quota-window-seconds 3600 \
  --digest-max-jobs-per-window 2
```

开启后，定时 scheduler 仍受 backlog 和 quota 保护。

## 手动调度

手动排队是默认推荐路径：

```bash
./scripts/pska --config .pska/config.json digest-schedule \
  --tenant-id tenant_graphintell \
  --owner-user-id test_user_3 \
  --limit 5 \
  --batch-size 1 \
  --quota-window-seconds 3600 \
  --max-jobs-per-window 2
```

如果只想检查或小批量推进，优先使用较小的 `limit` 和 `batch-size`。

`digest-now` 会先同步资料，再调度并运行一次 FastReAct digest worker。它比
`digest-schedule` 更容易立即消耗 LLM/API 吞吐，所以建议只在明确需要时使用。

## 已有保护

- 去重：当前 source 已有 active/completed digest 覆盖时不会重复排队。
- 失败保护：失败或取消的 digest 不会无限自动重试，除非 source 更新或 `force=true`。
- backlog 保护：periodic scheduler 可用 `max_backlog_jobs` 防止堆积。
- quota 保护：显式开启 periodic scheduler 或手动调度时，可用
  `quota_window_seconds` + `max_jobs_per_window` 限制新建 job 频率。
- worker 隔离：PSKA local worker 默认 `--exclude-job-type digest_via_fastreact`。

## 仍未完成

- token/成本预算还没有在 PSKA scheduler 层强制执行。
- 产品 UI 还没有“暂停 digest / 调整 digest 配额 / 查看 per-user quota”的控制面板。
- FastReAct worker 侧仍需要独立控制并发、模型 token 上限和超时。

## 建议试点配置

本地 LLM/API 吞吐有限时，建议保持默认手动模式。若必须开启 periodic digest，
建议：

```text
digest interval: 300-900 seconds
digest limit: 5-20 source items
digest batch size: 1
max backlog jobs: 3-10
quota: 1-2 jobs / hour / user
```

大型导入后不要立即 `force` 全量 digest；应先让 embedding/index 就绪，再按 KB 或 source 小批量推进。

当前 Phase 2 主线仍是证据可信问答；digest 内容质量需要单独验收，不能作为
Quick Ask 可靠性的前置假设。
