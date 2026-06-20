# PSKA Product Design

日期：2026-06-18

## 产品定位

PSKA 是一个 Postgres-first 的个人知识基础设施，同时也是面向个人知识工作者的本地知识/写作工作台底座。它负责保存原始资料、规范化 source/document/chunk、维护权限、引用、review、audit、memory、profile、entities 和 hyperedges，并通过 HTTP API / MCP / 本地 Web UI 暴露给人类用户、FastReAct 和其他 client。

FastReAct 是独立的 agentic service layer。PSKA 调用 FastReAct 执行 LLM planning、tool orchestration、digest、抽取和问答；FastReAct 不直接访问 PSKA DB，只能通过 PSKA HTTP/MCP contract 读写资料。

Postgres 是 canonical DB。Markdown/Wiki/富文本视图不是事实源，但它们不应只是可选导出：它们是用户理解资料、组织知识、进行写作和审查证据的重要产品界面。

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

## 端到端产品流程

PSKA 的产品体验应分成后台知识加工和前台知识使用两条同时运行的线。用户可以显式选择资料范围，也可以默认使用当前 `cwd` 下已授权的文档集合；PSKA 负责把它们变成可检索、可追溯、可审查的长期知识底座。

后台线对应 HippoRAG 语境里的 offline stage，但在 PSKA 里它不是一次性离线构建，而是持续增量索引：

1. Scope selection：用户选择或默认使用当前 `cwd` 下的 notes/documents/code snippets。每个 source 保留 path、content hash、mtime、owner、visibility 和 connector metadata。
2. Incremental ingest：只处理新增、变更或权限变化的 source；删除、移动或 hash 变化要触发 chunk、embedding、entity/hyperedge evidence 和 index cache 的失效/重建。
3. Embedding + graph indexing：文档被 chunked、embedded，并抽取 entity/hyperedge/source_refs；HippoRAG-inspired offline index 把 chunk、entity、fact/hyperedge 和 evidence passage 连接起来。第一版可以请求级 rebuild，产品上应演进为持久化增量 index。
4. Idle digest/review：空闲时不盲目 digest 全库，而是基于相似性、时间窗口、实体共现、未 digest 状态、重要 source 和用户近期任务，挑选相关文档集合做 review/digest。产物先进入 review candidate，再由用户确认是否写入 memory/profile/graph。
5. Feedback loop：问答、写作建议、用户确认/拒绝、保存的对话产物都会回流为 source material 或 review signal，影响下一轮 digest、retrieval eval 和图谱更新。

前台线是 PSKA service 提供的日常能力：

1. Chat / QA：用户可以普通闲聊，也可以提出需要个人资料支撑的问题。FastReAct 负责路由和 agentic planning，判断是普通回答、PSKA direct retrieval、GraphRAG、多步查询，还是需要工具调用。PSKA 负责 ACL、citations、GraphRAG、memory/profile context、gaps/conflicts 和可保存的对话产物。
2. GraphRAG answer path：在线阶段对应 HippoRAG online retrieval。query 会先找 lexical/vector passage，再做 fact/entity linking、PPR 节点探索和证据 chunk 融合；如果没有可靠图信号，就退回普通 RAG。
3. Rich-text writer：用户在富文本编辑器中创作，PSKA 不是替用户重写整篇，而是扫描当前草稿和 selected text，找出可补充论据、引用、记忆、profile 约束或图谱关系的位置。
4. Selected-text suggestion：当前第一版是用户选中文本后把 selected text 作为查询，返回中文写作建议、citations、graph paths、memory/profile 使用说明、gaps/conflicts。下一步应从“手动选中文本查询”升级为“低打扰扫描 + 可接受/忽略的建议”。
5. Evidence inspector：所有聊天答案和写作建议都能展开证据，包括 source/chunk、graph edge、fact confidence、review 状态、是否来自 memory/profile、是否存在冲突或 grounding 缺口。

这个流程的关键产品约束是：后台可以持续增量变聪明，但前台必须始终可用。FastReAct、LLM 或 digest worker 离线时，PSKA 仍应提供 direct retrieval、已有 index 查询、corpus explorer、writer selected-text suggestion 的 deterministic fallback，并明确标注哪些能力暂不可用。

## 主产品形态

PSKA 需要区分两个界面层次：

- Admin Console：管理状态、source、connector、review、jobs、memory/profile 和 ops。它回答“系统是否健康、数据是否进来了、哪些候选需要处理”。
- User Workspace：面向日常知识使用，承载对话、资料浏览、证据检查、富文本写作和基于选中文本的建议。它回答“我现在如何和 PSKA 一起理解、写作、整理和决策”。

截至 2026-06-18，Admin Console 第一版已经可用，入口包括 `/console`、`/console/reviews`、`/console/search`、`/console/memory`、`/console/jobs` 和 `/console/sources`。这些页面证明 PSKA 的本地服务、Postgres、HTTP 鉴权和管理入口可跑通，但还不能代表最终用户产品已经可用。

下一轮产品迭代应转向 User Workspace：

1. Chat Workspace：用户能与 PSKA 对话，答案必须带 citations、graph evidence、memory/profile 使用说明、gaps/conflicts 和可保存的 conversation source material。
2. Corpus / Wiki Explorer：用户能看懂 Postgres 里的 source、document、chunk、citation、entity、hyperedge、memory、profile 是什么样的，能按来源、时间、实体、引用和关系浏览。
3. Writer Mode：用户能在富文本里写作、圈选文本，并请求 PSKA 基于已授权资料、记忆、profile、引用和图谱路径给出中文写作建议。
4. Evidence Inspector：任何回答、建议、memory、profile 或 graph edge 都应能展开出处、证据片段、置信度、review 状态和是否缺失 grounding。
5. Retrieval Quality Loop：产品上必须承认当前是 HippoRAG-inspired GraphRAG v0，而不是成熟 HippoRAG 2/GNN；它已有离线 fact/entity/passage 图索引、fact/entity embedding linking、PPR 融合和普通 RAG fallback，下一步通过 fixture、expected citations、rerank、PPR 参数调优和真实问题回放提升质量。

参考方向：

- gbrain：强调面向用户问题的合成答案、可读引用和缺口说明。
- llm_wiki：强调把资料转成可浏览、可维护的结构化知识视图。
- HippoRAG：强调长期记忆启发的图检索和 personalized PageRank 类多跳能力；PSKA 当前采用请求级 HippoRAG-inspired fact/entity/passage index、embedding linking 和 PPR fusion 作为 v0，不声称完整复现 HippoRAG 2。

## 人类日常入口

PSKA 的日常入口应围绕一个简单循环：

1. 看状态：服务、DB、FastReAct、connector、jobs、digest backlog 是否健康。
2. 看 inbox：新资料、失败任务、待 review 项、可行动建议。
3. 做 review：确认、拒绝、延后或应用 memory/profile/graph/action 候选。
4. 问问题：用 agentic search 获取带 citations、graph evidence、gaps/conflicts 的回答。
5. 继续积累：同步文件、导入 archive、让 digest 在预算内处理相关资料。

第一版已经通过 CLI 和本地 Admin Console 打通；下一阶段应把重心从管理入口转向 User Workspace，把对话、资料浏览、证据检查和写作建议串成用户每天愿意使用的主流程。

## 产品原则

- Private-first：权限、ACL、represented user 和 source refs 是 PSKA 核心边界。
- Evidence-first：回答、memory、profile 和 graph 都必须保留出处。
- Review-before-impact：高影响、低置信或会改变长期记忆的内容进入 review。
- Budget-aware：LLM digest、问答和再消化都必须有 token/frequency/scope 预算。
- Open-source-first：解析、watch、rerank、评测、daemon、UI 和 graph algorithm 优先采用成熟开源项目或库；PSKA 自己聚焦 canonical data model、权限、review/audit、service contract 和 FastReAct 边界。
- FastReAct-independent：FastReAct 可以失败或离线；PSKA 仍可检索、查看 index、管理 job backlog 和 review。
