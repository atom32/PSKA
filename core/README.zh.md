# PSKA Core

PSKA Core 负责 source-centric 知识模型、数据库迁移、本地 HTTP service、CLI、jobs、review/candidate 边界、搜索、local daemon 和 MCP 工具。

当前文档地图从这里进入：

- [中文文档索引](../docs/README.zh.md)
- [Documentation Index](../docs/README.md)

## 当前模型

运行时知识从一等公民 Knowledge Source / source 出发。config roots 只作为启动默认值和初始化 seed；运行时 source 与 sync 状态以数据库为准。file sync 会处理配置的 folder sources、可选 PDF/DOCX 文本抽取、workspace Twitter/X archive inbox，并用内容 hash 判断增量工作。

digest 路径也是增量式的。`digest-scheduler` 按间隔检查新 source 或改动 source，`digest-now` 则执行 sync 并手动处理一次 digest。FastReAct digest 失败或没有写 candidates 时，PSKA 会通过 diagnostics 和 fallback review 暴露。

## 常用命令

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
./start.sh
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

## 关键参考

- [API Reference](../docs/API_REFERENCE.md)
- [Architecture](../docs/ARCHITECTURE.md)
- [Operations Runbook](docs/operations-runbook-zh.md)
- [Online Service Contract](docs/service-contract-zh.md)
- [Product Design](docs/product-design-zh.md)
- [Architecture Status](docs/architecture-status-zh.md)
