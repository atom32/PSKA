# PSKA MVP 用户可用范围

归档历史 scope 文档。本文保留 MVP+ 阶段判断，不再是当前产品计划入口。当前入口见
[中文文档索引](../../README.zh.md) 和 [Product Design](../../../core/docs/product-design-zh.md)。

日期：2026-06-16

相关上层文档：

- [product-design-zh.md](../../../core/docs/product-design-zh.md)：完整产品设计和核心用户工作流。
- [architecture-status-zh.md](../../../core/docs/architecture-status-zh.md)：模块成熟度和当前技术缺口。
- [todo-implement-system-zh.md](todo-implement-system-zh.md)：当前可执行 TODO、选择规则和验收门禁。

## 判断

PSKA 的 MVP 不应该继续优先横向扩 connector。更多 connector 会带来更多入口，但对核心输出格式没有本质变化：最后都应落到 source item、document、chunk、source refs、candidate knowledge、review、memory、search/QA。

MVP 当前应收窄数据来源，把精力集中在纵向闭环：

```text
有限高价值数据源
  -> Postgres-first source/chunk/citation
  -> LLM/Fastreact 抽取、digest、候选写回
  -> PSKA review/audit/memory/graph
  -> retrieval / agentic QA / briefing
  -> local service daemon 持续服务用户
```

## MVP 数据源 Scope

第一阶段只保留两类真实数据源：

- Twitter/X archive：作为主要个人公开/半公开知识流，覆盖大量兴趣、项目、观点和外部链接。
- 本地文本文件：作为用户主动整理的 notes、Markdown、JSON/YAML、代码片段和轻量文档入口。

暂缓：

- Mail、photos、NAS、Home Assistant、浏览器历史、GitHub 深度同步、PDF/Word 复杂版面解析/OCR。
- 这些后续都应复用同一 connector record/state contract，而不是现在抢占 MVP 注意力。

## MVP 功能 Scope

MVP+ 的重点不是“能导入所有东西”，而是这些能力真实可用：

- 能长期运行：`local-daemon` 启动 HTTP service、job worker、digest scheduler。
- 能持续积累：新 source 进入 Postgres，保留 source refs 和 citation。
- 能抽取：LLM/Fastreact 生成 entities、hyperedges、review/memory/profile candidates。
- 能审查：高影响内容进入 review，不直接变成长期记忆。
- 能回答：search/agentic-search 带 citations、graph evidence、gaps/conflicts/sensitivity。
- 能恢复：jobs 有 status、retry、recover stale、events。
- 能观测：`/ready`、`/metrics`、job stats 能定位服务、worker、Fastreact、connector 状态。

## 非目标

MVP 阶段不追求：

- 完整 UI。
- 全量 connector 生态。
- GNN 或 HippoRAG/PPR 级 GraphRAG。
- 后台系统级安装器和复杂日志轮转。
- 自动执行高影响动作。

实现原则：

- Open-source-first：非 PSKA 核心边界能力优先采用成熟开源项目或库，不从头造轮子。
- PSKA 自己实现的重点是权限、source refs、review/audit、canonical DB、service contract 和 FastReAct 边界。
- 文档解析、网页抽取、watch、rerank、评测、daemon 包装、UI 组件和 graph algorithm 都应先评估现成项目，再决定是否做薄封装。

## 当前下一步

当前推进以 [todo-implement-system-zh.md](todo-implement-system-zh.md) 的结构化 backlog 为准。`Human Workflow` 第一轮已经完成，MVP+ 已经具备每日状态、deterministic briefing、optional narrative briefing、review summary、memory/profile read-only view、agent conversation capture、grounded relationship apply 和 digest budget policy 的第一版。

已完成任务：

1. `HW-001` Daily Status Entry。
2. `HW-002` Review Summary。
3. `HW-003` Memory/Profile Read-only View。
4. `HW-004` Deterministic Daily Briefing v0。
5. `HW-005` FastReAct Narrative Briefing。
6. `HW-006` Agent Conversation Capture。
7. `HW-007` Grounded Graph Candidate Review。
8. `HW-008` Digest Budget Policy。

下一轮结构化 TODO 已根据真实使用反馈和架构状态补充到 [todo-implement-system-zh.md](todo-implement-system-zh.md)。当前 `ready` tasks 从 `HW-009` Digest E2E Write-back Gate 开始，随后可按依赖继续 `HW-010` Review Batch Operations、`HW-013` Human-readable Ops Briefing 和 `HW-014` Local Daemon Productization。

历史技术优先级仍保留为背景：

1. 先用有限数据集跑稳真实 digest E2E：PSKA backlog -> Fastreact worker -> candidates/review/memory/profile 写回 -> job complete。
2. 修 review taxonomy 和 apply path，让 digest 结果能被人类确认、拒绝、批量处理，并保留 audit/source refs。
3. 把 memory promotion 做成稳定生命周期：candidate -> review -> agent memory/profile/hyperedge。
4. 打磨 retrieval/GraphRAG 质量：先做 fixture/evaluation/rerank，再考虑 HippoRAG/PPR；暂不做 GNN。
5. 把 `local-daemon` 从前台 supervisor 产品化到可日常运行：日志、restart、status、配置检查。
6. 固化人类日常 workflow：`files-sync`/`files-watch`、`digest-schedule`、`mvp-status --summary`、`review-list`、`agentic-search`、daily briefing/inbox。
7. 只在核心 digest/review/memory/retrieval 闭环稳定后再扩 Browser/Git connector。

推荐本地路径：

```bash
./scripts/pska mvp-bootstrap \
  --twitter-archive ~/PSKA_workspaces/default/twitter_archive \
  --notes-root ~/Documents/notes \
  --extract

./scripts/pska mvp-status --summary

./scripts/pska files-sync

./scripts/pska local-daemon
```
