# PSKA Product Design

日期：2026-06-17

## 产品定位

PSKA 是一个 Postgres-first 的个人知识基础设施。它负责保存原始资料、规范化 source/document/chunk、维护权限、引用、review、audit、memory、profile、entities 和 hyperedges，并通过 HTTP API / MCP 暴露给人类用户、FastReAct 和其他 client。

FastReAct 是独立的 agentic service layer。PSKA 调用 FastReAct 执行 LLM planning、tool orchestration、digest、抽取和问答；FastReAct 不直接访问 PSKA DB，只能通过 PSKA HTTP/MCP contract 读写资料。

Markdown/Wiki 是可选 curated view，不是事实源。Postgres 是 canonical DB。

## 第一用户和目标

PSKA 的第一用户是个人知识工作者，也就是当前项目的实际使用者。产品目标不是“导入所有资料”，而是让有限高价值资料能够被长期沉淀、持续消化、可追溯检索、可审查地进入长期记忆和图谱。

MVP+ 数据范围先收敛到：

- Twitter/X archive：个人公开/半公开知识流、兴趣、观点、项目线索和外部链接。
- 本地文件/notes root：用户主动整理的 notes、Markdown、JSON/YAML、代码片段和轻量文档。

## 核心用户工作流

```text
导入原始资料
  -> PSKA 写入 source/document/chunk/citation
  -> 抽取 entity/hyperedge/review candidate
  -> 空闲 digest 做受预算约束的关联再消化
  -> 人类每天查看 briefing/inbox/review
  -> 需要时通过 FastReAct 做带引用问答
  -> 对话产物回流为 source material
  -> 确认后的候选进入 memory/profile/graph
```

具体行为：

- 导入原始资料时，PSKA 保留原始来源、时间、connector metadata、content hash、visibility、owner 和 source refs。
- 系统抽取 `source`、`chunk`、`entity`、`hyperedge`、`review candidate`。所有生成知识都必须能追溯到 source refs。
- 空闲 digest 不是一次性处理所有文档，也不是无限后台消耗。它应基于标签、相似性、时间线、实体共现和隐藏关联，选择相关资料重新联想、重新消化，并严格受 token budget、频率、窗口、去重和最大资料集合大小限制。
- 人类每天查看 briefing、inbox、review，而不是直接读 job log。
- 需要时通过 FastReAct 做带引用问答。由 PSKA 调用 FastReAct 产生的重要对话、答案、引用和 trace summary，也应作为新的 source/material 被 PSKA 存档，进入后续检索、digest 和 review 流程。
- 重要候选被确认后进入 memory、profile、graph。进入 graph 的不是孤立结论，而是带原始出处、置信度、review 状态和关系语义的关联数据，方便构成可追溯联系并提升后续查询。

## 人类日常入口

PSKA 的日常入口应围绕一个简单循环：

1. 看状态：服务、DB、FastReAct、connector、jobs、digest backlog 是否健康。
2. 看 inbox：新资料、失败任务、待 review 项、可行动建议。
3. 做 review：确认、拒绝、延后或应用 memory/profile/graph/action 候选。
4. 问问题：用 agentic search 获取带 citations、graph evidence、gaps/conflicts 的回答。
5. 继续积累：同步文件、导入 archive、让 digest 在预算内处理相关资料。

第一版可以通过 CLI 完成；UI 可以后置。

## 产品原则

- Private-first：权限、ACL、represented user 和 source refs 是 PSKA 核心边界。
- Evidence-first：回答、memory、profile 和 graph 都必须保留出处。
- Review-before-impact：高影响、低置信或会改变长期记忆的内容进入 review。
- Budget-aware：LLM digest、问答和再消化都必须有 token/frequency/scope 预算。
- Open-source-first：解析、watch、rerank、评测、daemon、UI 和 graph algorithm 优先采用成熟开源项目或库；PSKA 自己聚焦 canonical data model、权限、review/audit、service contract 和 FastReAct 边界。
- FastReAct-independent：FastReAct 可以失败或离线；PSKA 仍可检索、查看 index、管理 job backlog 和 review。

