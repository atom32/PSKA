# PSKA MVP 用户可用范围

日期：2026-06-16

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

- Mail、photos、NAS、Home Assistant、浏览器历史、GitHub 深度同步、PDF/Word 复杂解析。
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

## 当前下一步

优先顺序：

1. 把 `local-daemon` 跑成稳定的本地前台服务入口。
2. 用有限 Twitter/X archive 样本和少量本地 notes 持续验证 digest/review/memory。
3. 打磨 retrieval/GraphRAG 质量和 review taxonomy。
4. 只在核心流程稳定后再扩 Browser/Git/PDF 等 connector。
