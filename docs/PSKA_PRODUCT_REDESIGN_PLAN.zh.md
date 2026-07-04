# PSKA 产品重设计计划

本文是比 `PSKA_MULTI_KB_PRODUCT_PLAN.zh.md` 更高一层的产品重设计方案。它把“多知识库 RAG”放回完整产品中看：PSKA 应该借鉴腾讯 ima / WeKnora 的知识库、问答、搜读写和共享工作台，同时保留自己的证据治理、图谱、写作和 PSKA / FastReAct / AuthNode 分离式架构。

重要前提：PSKA 的 Graph、Writing、Digest/Review 等差异化能力很有潜力，但有效性需要理论和实证验证。产品设计上不能把它们包装成必然正确的魔法，而要把它们设计成可解释、可回退、可度量的增强层。

## 1. 产品定位

### 一句话

PSKA 是一个以证据为中心的个人/团队知识工作台：先像 ima 一样把资料变成可搜索、可问答、可写作的知识库，再通过 Review、Graph 和 Writing 把证据逐步沉淀成可复用的理解资产。

### 不是

- 不是只做一个向量搜索壳。
- 不是只做 ChatGPT 文件问答。
- 不是默认相信自动抽取的知识图谱。
- 不是把所有 agent 输出直接写进长期记忆。
- 不是把 Auth、Agent、Knowledge 都塞进一个单体。

### 是

- 一个多知识库 RAG 产品。
- 一个证据可追溯的 Ask / Read / Write 工作台。
- 一个把“回答”转化为“可审阅知识”的系统。
- 一个可以用 FastReAct 做复杂任务、用 AuthNode 做身份边界、用 PSKA 做证据和知识边界的组合式平台。

## 2. 参考对象与取舍

### 从 ima 借鉴

公开产品信息显示，ima 的核心心智是 AI 知识库与“搜、读、写”一体化：用户收集资料，建立个人知识库或加入共享知识库，然后围绕资料做搜索、问答、阅读和写作。它还有知识库广场、任务模式、笔记、录音纪要、AI 解读等偏产品化入口。

PSKA 应借鉴：

- 知识库作为第一产品对象。
- 新建对话和知识库入口足够轻。
- 用户能快速选择问答范围。
- 文件、网页、笔记、音频等材料都应进入统一资料入口。
- 读资料和问资料之间不要割裂。
- 分享/协作知识库可以成为增长和团队使用入口。

PSKA 不应照搬：

- 以“广场/公开知识库”为第一阶段重心。
- 自动 Wiki 或自动图谱的确定性表达。
- 过重的企业连接器矩阵作为 MVP 前提。

### 从 WeKnora 借鉴

WeKnora 给了开源实现参考：

- `knowledge_bases -> knowledges -> chunks` 的一等 KB 数据模型。
- KB 详情页、KB 列表、KB selector、chat 多 KB 选择。
- RAG quick QA、Agent mode、Wiki mode 三层能力。
- 多租户 RBAC、共享空间、KB 所有权。
- MCP、模型、向量库、数据源、Langfuse observability 等平台化配置。
- 文档处理进度、chunk preview、reparse、move/copy、pin 等产品细节。

PSKA 应借鉴这些工程骨架，但保留自己的边界：

- PSKA 负责证据、引用、检索、长期知识治理。
- FastReAct 负责复杂 agentic loop，不拥有 PSKA 的长期知识库。
- AuthNode 负责身份、租户、组织和 token，不和 RAG 存储耦合。
- 自动生成的 claim/graph/memory 必须经过 evidence check 和 Review。

## 3. 产品北极星

### North Star

用户能把分散资料放进可信知识库，并在任意工作流中得到可追溯、可验证、可继续写作的答案。

### 核心价值指标

- `Time to grounded answer`：从导入资料到得到带 citation 答案的时间。
- `Supported answer rate`：Ask 回答通过 evidence check 的比例。
- `Citation precision`：用户点击引用后认为相关的比例。
- `No-answer correctness`：无答案时没有编造，并能解释原因的比例。
- `Review acceptance rate`：自动候选被用户批准/应用的比例。
- `Writing reuse rate`：Ask/Digest 证据进入 Writing draft 的比例。
- `Graph usefulness rate`：图谱路径被用户采纳为理解线索的比例。
- `Scope leak incidents`：跨 KB/跨权限错误引用次数，目标为 0。

### 设计原则

1. 证据先于结论。
2. 知识库先于图谱。
3. 可解释先于自动化。
4. Review 先于长期记忆。
5. 快速问答和深度任务分层。
6. 分离式架构优先于大单体。
7. 实验能力必须有指标和回退路径。

## 4. 目标用户与任务

### 个人知识工作者

任务：

- 收集论文、网页、PDF、聊天记录和笔记。
- 快速查找某个信息。
- 比较多个资料源观点。
- 生成读书笔记、研究 brief、写作草稿。

产品重点：

- 快速导入。
- 可靠问答。
- 引用可点击。
- 写作可继续。

### 小团队 / 项目组

任务：

- 建项目知识库。
- 共享文档和 FAQ。
- 统一回答标准。
- 审核自动沉淀的知识。

产品重点：

- 多知识库。
- 权限和共享。
- Review 队列。
- 团队可见的 evidence brief。

### 高阶研究/运营用户

任务：

- 长期追踪主题。
- 发现关系、冲突和变化。
- 组织资料成为文章、报告或决策 brief。

产品重点：

- Digest。
- Graph。
- Writing Workspace。
- Deep Research / FastReAct 任务。

## 5. 新产品信息架构

建议最终 IA：

```text
Home / Today
Ask
Knowledge Bases
Reader
Research Tasks
Graph
Writing
Review
Settings / Admin
```

### Home / Today

定位：每日工作入口，不是单纯 dashboard。

内容：

- 最近对话。
- 最近知识库。
- 正在处理的资料。
- 待 Review 候选。
- Digest 发现。
- 未完成 Writing 项目。
- 系统 readiness 问题。

关键动作：

- 新建 Ask。
- 上传资料。
- 继续 Writing。
- 处理 Review。
- 查看最近 Digest。

### Ask

定位：主要问答与深度任务入口，参考 ima 的“问知识库”心智。

核心交互：

- 输入框。
- Scope picker：全部 / 当前 KB / 多 KB / 附件 / Writing context。
- Quick / Deep / Auto。
- 答案 + citations + source windows。
- RAG pipeline progress。
- no-answer diagnostics。
- 保存为 Evidence Brief / Writing node / Review candidate。

设计要求：

- 默认 quick，保证低延迟。
- deep 用 FastReAct，展示 agent steps。
- 所有答案必须显示 retrieval owner：`pska` 或 `fastreact_pska_mcp`。
- scope 必须显式可见。

### Knowledge Bases

定位：资料容器和 RAG readiness 管理入口。

页面：

- KB 列表。
- KB 详情。
- 资料 tab。
- 处理 tab。
- Ask tab。
- Digest tab。
- Graph/Memory tab。
- Writing tab。
- 设置 tab。

关键动作：

- 创建 KB。
- 上传文件。
- 粘贴文本。
- 添加 URL/RSS/folder。
- 同步。
- move/copy/reparse。
- 查看 chunks。
- 检查 embedding coverage。
- 运行 Digest。

### Reader

定位：从“问资料”补齐到“读资料”。

Reader 可以作为 KB 详情中的资料阅读模式，也可以成为独立 surface。

能力：

- 原文预览。
- chunk 高亮。
- citation 回跳。
- 章节/页码/表格定位。
- 对选中文本 Ask。
- 保存摘录。
- 生成笔记。
- 标记“需要 Review”。

这是 ima “读”的关键体验，PSKA 现在还偏 Ask 和管理，应补上阅读层。

### Research Tasks

定位：深度、多步、可追踪任务入口，由 FastReAct 执行。

任务类型：

- 深度问答。
- 多资料比较。
- 主题综述。
- 冲突检查。
- 资料更新监控。
- Web + KB 联合研究，后续。

原则：

- 任务可以长跑。
- 每一步保存 trace。
- 所有外部发现要回到 PSKA 做 source_refs / evidence check。
- 写入长期知识必须走候选和 Review。

### Graph

定位：实验性理解层，不是事实数据库。

Graph 展示：

- entities。
- claims。
- digest notes。
- relationships。
- source evidence。
- conflicts。
- review status。

交互：

- 按 KB 过滤。
- 从 answer/citation 跳到 graph。
- 从 graph path 回到 source windows。
- 标记“有用/无用/错误”。
- 把路径保存为 Writing evidence。

产品文案应避免“系统已经理解一切”。推荐表达：

- “证据关系”
- “候选关联”
- “可审阅路径”
- “基于来源的推断”

### Writing

定位：把 Ask/Digest/Graph 产生的证据组织成草稿。

继续保留 Inquiry Graph，但重新解释为：

- 问题网络。
- 证据板。
- 答案节点。
- gap 节点。
- section/draft 节点。

关键规则：

- Writing compose 不重新检索，只使用已保存的 answer/evidence nodes。
- Follow-up Ask 可以带 board context 和 KB scope。
- 每个 answer node 保留 Ask run、source_refs、evidence_check。
- 用户能从 citation 回到 Reader。

### Review

定位：长期知识写入闸门。

Review 对象：

- memory candidate。
- profile update。
- relationship candidate。
- knowledge claim。
- stale evidence。
- conflict。
- high-impact action。

动作：

- approve。
- reject。
- approve and apply。
- request more evidence。
- convert to writing note。

Review 是 PSKA 区别于普通 RAG 的关键，不应隐藏在后台。

### Settings / Admin

模块：

- 身份与租户，由 AuthNode 提供。
- 模型配置。
- embedding/retrieval。
- parser/chunking。
- MCP tools。
- FastReAct connection。
- data retention。
- audit log。
- import/export。

## 6. 关键端到端体验

### Flow 1：从资料到可信回答

```text
创建 KB -> 上传资料 -> processing timeline -> embedding/readiness -> Ask -> citations/source windows -> Reader 验证
```

成功标准：

- 用户知道资料有没有入库。
- 用户知道答案来自哪些资料。
- 用户能点击回原文。
- 找不到答案时不会编造。

### Flow 2：从回答到写作

```text
Ask -> supported answer -> 保存到 Writing -> 自动带 citations/source_refs -> 组织 section -> compose draft
```

成功标准：

- 草稿每个关键结论都有来源。
- compose 不偷偷新增无来源内容。
- 用户能从草稿回到证据。

### Flow 3：从资料到长期知识

```text
KB Digest -> candidate claims/relationships/memories -> Review -> apply -> Graph/Memory
```

成功标准：

- 自动产物不直接污染长期知识。
- Review 卡片解释候选为何出现。
- apply 后仍保留 source_refs。

### Flow 4：深度研究任务

```text
用户发起 Deep Research -> FastReAct 计划和调用工具 -> PSKA read-only MCP 检索 -> 结果回写 PSKA run -> evidence check -> brief/report
```

成功标准：

- FastReAct 不能绕过 PSKA 权限。
- Deep 结果有 trace 和 citations。
- 失败时可解释。

### Flow 5：图谱辅助理解

```text
Ask/Digest/Review 产生证据 -> Graph 生成候选路径 -> 用户检查 path -> 保存或纠错
```

成功标准：

- 图谱路径可回到原文。
- 错误路径可被标记。
- 指标证明它提高了理解/写作效率，否则保持实验入口。

## 7. 产品层级与命名

### 推荐命名

- `知识库`：Knowledge Base，用户创建的 corpus。
- `资料`：source item / document，用户导入的原始材料。
- `片段`：chunk/passage，检索单元。
- `证据`：source_ref/citation，能支撑回答的引用。
- `理解`：Digest/claims/relationships 的统称，但必须是可审阅。
- `图谱`：证据关系图，不叫“事实图谱”。
- `写作`：Writing Workspace / Inquiry Graph。
- `深度任务`：FastReAct-backed research tasks。

### 避免命名

- 不把 `KnowledgeSource` 暴露为“知识库”。
- 不把未 Review 的 graph edge 称为“事实”。
- 不把 Deep answer 称为“已验证报告”，除非 evidence check 通过。

## 8. 服务分离架构

### AuthNode

职责：

- 登录、SSO、OIDC。
- tenant/org/user/team。
- role、group、membership。
- JWT/session。
- service token。
- audit identity。

不负责：

- RAG 检索。
- 文档 chunking。
- agent trace。
- graph/memory 写入。

### PSKA

职责：

- knowledge base / source / document / chunk canonical DB。
- ingestion、parser、chunking、embedding。
- retrieval、hybrid search、scope resolution。
- citations、source windows、evidence check。
- Ask quick。
- Review、Digest artifacts、Graph/Memory canonical store。
- Writing boards 和 evidence brief。
- read-only MCP tools 给 FastReAct。
- write-back APIs 接收 FastReAct 候选。

不负责：

- 复杂多步 agent planning。
- 外部账户登录。
- 任意第三方 tool sandbox。

### FastReAct

职责：

- Deep Ask / Research Tasks。
- ReAct planning。
- tool orchestration。
- web/tool/MCP 调用。
- 长任务 trace。
- 复杂综合。

约束：

- 读取 PSKA 资料必须走 PSKA MCP tools。
- 写回长期知识必须走 PSKA candidate APIs。
- 不直接写 PSKA graph/memory。
- 每次 run 必须带 tenant/user/represented_user/scope。

### 前端 / Gateway

职责：

- 提供统一工作台体验。
- 通过 AuthNode 建立身份。
- 通过 PSKA API 访问知识库和证据。
- 通过 PSKA 发起 FastReAct-backed task，而不是直接调用 FastReAct。

### 服务调用图

```text
Frontend
  -> AuthNode: login/session/token
  -> PSKA: KB/Ask/Review/Writing/Graph APIs
       -> FastReAct: deep task request
            -> PSKA MCP: read evidence/search/context
       <- FastReAct: trace/result/candidates
  <- PSKA: evidence-checked result
```

### 本地仓库边界

- PSKA 与 AuthNode 集成相关改动在 `/Users/xudawei/Documents/personal archive` 里完成；AuthNode 只应提供身份、租户、组织、team、token 和 audit identity，不承载知识库 membership 或 RAG 存储。
- FastReAct 相关改动在 `~/FastReAct` 里完成；FastReAct 只接收 PSKA 传入的 tenant/user/scope，复杂任务读取资料必须走 PSKA MCP/API，不能直接拥有或绕过 PSKA 的长期知识库。
- 当前 Phase 1 多知识库 RAG 底座只需要 PSKA 本仓库改动；只有当 deep Ask / Research Task 需要真实传递 `knowledge_base_ids` 到 agentic loop 时，才进入 FastReAct 仓库同步协议。

## 9. 数据对象重构蓝图

核心对象：

```text
Tenant / User / Team / Space
KnowledgeBase
KnowledgeBaseMembership
KnowledgeSource
SourceItem
Document
Chunk / PassageWindow
AskConversation / AskRun / AskMessage
EvidenceRef / Citation
DigestNote / KnowledgeClaim / ReviewItem
Entity / Relationship / GraphPath
WritingBoard / WritingNode / WritingEdge
ResearchTask / AgentTrace
PromptProfile
```

关键关系：

- KB 包含 source memberships。
- source 产生 documents/chunks。
- AskRun 记录 KB scope 和 evidence refs。
- Digest/Review/Graph/Writing 都记录 source_refs 和 KB lineage。
- ResearchTask 记录 FastReAct trace，但 evidence owner 仍是 PSKA。

详细 schema 和 API 可继续使用 `PSKA_MULTI_KB_PRODUCT_PLAN.zh.md` 作为下钻设计。

## 10. 前端重设计方向

### 最终导航

如果彻底重做 UI，推荐左侧主导航：

```text
新对话
Today
知识库
阅读
深度任务
图谱
写作
Review
设置
```

但迁移时不需要一次性替换当前工作台。建议两阶段：

1. 当前 React 工作台内嵌 KB scope 和 KB rail。
2. 稳定后重新整理导航，把 Ask 和 Knowledge Bases 提升为更强入口。

### 页面布局原则

- SaaS/工作台风格，密度高、安静、可扫描。
- 不做营销 hero。
- 核心页面优先支持重复操作。
- 引用和证据永远在答案旁边，不藏到 debug。
- Agent trace 可以折叠，但用户能展开检查。
- Graph/Writing 是工作区，不是装饰性可视化。

### 关键组件

- GlobalScopePicker。
- KnowledgeBaseRail。
- KnowledgeBaseCard。
- SourceIngestPanel。
- ProcessingTimeline。
- ReaderPane。
- AskComposer。
- CitationInspector。
- EvidenceCheckBadge。
- ResearchTaskTimeline。
- ReviewQueueCard。
- GraphEvidencePathPanel。
- WritingEvidenceNode。

## 11. PSKA 差异化能力的验证计划

### Graph 验证

假设：

Graph 能帮助用户发现跨资料关系、冲突和主题结构。

风险：

- 自动关系错误。
- 图谱噪音太大。
- 用户不需要可视化图。

验证：

- Graph path click-through rate。
- Path saved to Writing rate。
- User-marked useful/wrong ratio。
- Review approval rate for relationship candidates。
- 有/无 Graph 的研究任务完成时间对比。

产品策略：

- 第一阶段作为辅助视图。
- 所有 edge 带 evidence refs。
- 错误可反馈。
- 不把 graph result 作为无证据回答来源。

### Writing 验证

假设：

Inquiry Graph 比线性 chat 更适合组织长文、报告和研究 brief。

风险：

- 用户觉得画布太重。
- 自动建议的问题质量不稳定。
- 草稿组织成本高于收益。

验证：

- Ask answer saved to board rate。
- Board-to-draft conversion rate。
- Draft citation completeness。
- 用户编辑后保留率。
- 写作任务完成时间。

产品策略：

- 先从 Evidence Brief 和 answer-to-draft 开始。
- Inquiry Graph 作为高级模式。
- 保持 compose retrieval-free。

### Digest/Review 验证

假设：

自动候选 + 人类 Review 能让长期知识更可靠。

风险：

- Review 太重。
- 候选质量不够。
- 用户不愿维护长期知识。

验证：

- Candidate approval rate。
- Rejection reasons。
- Review queue aging。
- Applied knowledge reuse in Ask。

产品策略：

- 只对高价值/高置信候选提示 Review。
- 批量操作。
- 从 Digest summary 进入，而不是制造噪音。

## 12. 分阶段路线图

### Phase 0：产品定义与 IA 冻结，1 周

产出：

- 本文档评审。
- 核心 IA 和命名确认。
- WeKnora/ima 对照表。
- 当前前端迁移策略确认。

验收：

- 团队能回答 PSKA 与 ima/WeKnora 的同异。
- 多知识库是主线，Graph/Writing 是增强层。
- 分离式架构边界明确。

### Phase 1：多知识库 RAG 底座，2-3 周

产出：

- `knowledge_bases` 和 membership。
- default KB backfill。
- KB CRUD。
- KB-scoped source upload/text/url/rss/folder。
- Ask `knowledge_base_ids` scope。
- KB readiness。
- 前端 KB rail/scope picker。

验收：

- 一个账号多个 KB。
- Ask 不跨 KB 泄露 citation。
- 默认 RAG 可用。

### Phase 2：搜读问一体化，2-3 周

产出：

- ReaderPane。
- CitationInspector。
- source window -> 原文回跳。
- Ask 页面从 Today 中独立增强。
- ProcessingTimeline。
- no-answer diagnostics 产品化。

验收：

- 用户能从答案一路回到原文。
- 用户能从原文选中文本提问。
- 处理失败可解释。

### Phase 3：Deep Research 与 FastReAct 产品化，2-4 周

产出：

- ResearchTask 表和 API。
- FastReAct run trace 显示。
- Deep task queue。
- PSKA MCP scope 透传产品化和回归监控。
- deep result evidence check。

验收：

- Deep task 不绕过 PSKA 权限。
- Deep 失败可解释。
- Deep 输出可保存为 Brief/Writing。

### Phase 4：Digest/Review/Writing 闭环，3-5 周

产出：

- KB-scoped Digest。
- Review Center 按 KB 过滤。
- Evidence Brief 2.0。
- Writing board 默认 KB scope。
- Answer-to-writing flow。

验收：

- Ask/Digest 结果能进入写作。
- 长期知识写入经过 Review。
- Draft 保留 citations。

### Phase 5：Graph 实验层，2-4 周

产出：

- KB-filtered graph。
- Evidence path inspector。
- relationship candidate review。
- useful/wrong feedback。
- graph usefulness metrics。

验收：

- 图谱所有路径可追溯原文。
- 错误路径能反馈。
- 有初步指标判断是否继续投资。

### Phase 6：协作与共享，后续

产出：

- AuthNode-backed tenant/org/team。
- KB share/read-only/editor。
- Shared KB list。
- 审计日志。
- 知识库模板/广场，谨慎推出。

验收：

- 团队共享 KB 可用。
- 权限边界清晰。
- 共享不会降低 evidence/citation 约束。

## 13. 近期执行建议

当前 `tenant` 分支已经从计划进入 Phase 1 实现：DB/API/store、前端 KB rail/scope picker、documents membership 管理、Ask/Digest/Review/Graph/Writing 的 KB scope 传递，以及真实迁移/scoped Ask smoke 都已有落地。额外地，真实 `~/FastReAct` + PSKA HTTP MCP + DeepSeek smoke 已验证 deep Ask 会把选中 KB hard scope 注入 PSKA MCP tool calls，未选 KB 不进入答案或 trace。接下来建议立刻做三件事：

1. 把 `docs/MILESTONE_PHASE1_MULTI_KB_RAG.zh.md` 作为 Phase 1 验收包主文档，逐项对齐证据、测试和已知非目标。
2. 补齐 release notes 的 KB scope contract，保证前端、MCP、FastReAct 后续接入使用同一语义。
3. 跑全量 core/frontend/FastReAct 验证后，决定 Phase 1 是否把 FastReAct deep scope 透传作为正式发布项，还是以提前打通的实验能力标注。

原因：

- 没有 KB 底座，ima 式产品心智无法成立。
- 没有 Ask scope，Graph/Writing 都会有证据边界问题。
- 没有 readiness，RAG 质量无法产品化。
- 如果 deep Ask 与 quick Ask 的 scope 语义不一致，产品上会出现“同一个选择器两种答案边界”的风险。

## 14. 最小可发布定义

第一版“重新产品化”的最小发布，不要求 Graph/Writing 全部成熟，但必须做到：

- 用户能创建多个知识库。
- 用户能导入资料并看到处理状态。
- 用户能选择知识库提问。
- 答案有 citations/source windows。
- 找不到答案时有 diagnostics。
- Deep Ask 可选且失败可回退。
- Review 仍是长期知识写入闸门。
- AuthNode/PSKA/FastReAct 边界不混乱。

达到这个状态后，PSKA 才有资格继续强化 Graph、Writing、Digest 等独特能力。

## 15. 开放问题

- PSKA 面向个人优先，还是团队优先？
- 是否要做 ima 式知识库广场？如果做，是公开广场还是组织内模板库？
- Graph 是默认入口还是高级实验入口？
- Writing 是主导航还是从 Ask/Brief 渐进进入？
- FastReAct deep task 是否允许 web search 默认开启？
- AuthNode 是否作为所有部署的必选服务，还是本地模式可降级？
- per-KB embedding/chunking 配置第一版是否开放给用户？

这些问题不阻塞 Phase 1，但会影响 Phase 3 之后的产品取舍。
