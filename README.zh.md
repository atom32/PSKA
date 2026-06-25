# PSKA

PSKA 是一个 private-first 的本地个人知识工作台。它把本地文件和 Twitter/X
归档导入 PostgreSQL，规范化为 source/document/chunk，提供搜索、review、
digest、HTTP API 和 MCP，并在配置后把复杂 agentic digest 交给 FastReAct。

## 从这里开始

完整文档地图：

- [中文文档索引](docs/README.zh.md)
- [Documentation Index](docs/README.md)

最短本地流程：

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
./start.sh
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json mvp-status --summary
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

`db-reset` 是破坏性操作，会删除并重建指定的本地数据库。只在你明确想重新冷启动时使用。

`./start.sh` 读取 `.pska/config.json`，按配置准备知识来源、启动后端 supervisor，并在启用时启动前端 Workspace。

`digest-now` 会先跑 file sync。sync 路径覆盖 folder sources、PDF/DOCX/XLSX 文本抽取、可选 legacy XLS 抽取、workspace 的 Twitter/X archive inbox，以及基于内容 hash 的增量处理。随后它会调度并处理一次 digest。

## 日常使用

当前后台 digest scheduler 是增量轮询，不是每天固定时间的 cron。`./start.sh`
启动 local daemon 后，`pska-digest-scheduler` 默认每 300 秒检查一次有没有新 source 或已改动 source。已经被当前 digest job 覆盖的 source 会跳过，除非内容发生变化或手动命令使用 `--force`。

常用命令：

```bash
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json daily-briefing --owner-user-id user_primary --limit 5
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

如果 FastReAct 处理了 digest job 但没有写入 candidates，PSKA 会通过 diagnostics 和 fallback review 暴露出来，不再静默显示 0 review。

## 主要组件

- `core/`：source-centric 知识模型、摄入、jobs、搜索、review、HTTP API、local daemon 和 MCP 工具。
- `frontend/`：User Workspace scaffold，已有 Today、Discoveries、Corpus、Graph 和 review 相关页面；Knowledge Sources/file management UI 仍是下一步。
- `channels/twitter-x/`：Twitter/X 归档采集器和 archive schema。

## 运行时数据

运行时/用户数据默认位于 `~/PSKA_workspaces/default`。imports、logs、run files 和 active workspace data 不应放进源码仓库。

`.pska/config.json` 是本机配置，不要提交。config 只作为启动/default seed；运行时 source 和 sync 状态以数据库为准。

## 许可证

MIT
