# PSKA Roadmap / TODO

日期: 2026-06-10

## 当前判断

PSKA 已完成初期 MVP 闭环：Twitter/X 归档、Postgres 存储、LLM 提取、轻量超图、ACL 优先检索、agentic QA、MCP、Fastreact 联动和 HTML 验收报告都已经跑通。

下一阶段重点不是继续堆演示，而是把 MVP 变成可长期使用的家庭服务器系统。核心方向是：

- 统一使用 Python 3.12 wrapper/venv，避免宿主 Python、FlagEmbedding、PyTorch 版本漂移影响可安装性。
- 让检索质量真实提升，而不是依赖占位 lexical/semantic 评分。
- 让导入、抽取、索引成为可恢复的后台任务。
- 让用户可以审核、批准、撤销和理解 Agent 写入的知识。
- 把对话、文件和未来 channel 都纳入同一数据/权限/记忆体系。
- 给 PSKA 一个本地 UI，让系统可观察、可管理、可纠错。

## P0: 下一阶段必须优先做

### P0.1 真实 embedding pipeline（已验收）

目的：让语义检索从 placeholder 变成真正可用的 hybrid retrieval。

当前状态：已实现并完成本地验收。默认模型路线为本地 `BAAI/bge-m3`，schema 为 `vector(1024)`，提供导入时嵌入、批量 backfill、query embedding、Postgres/pgvector vector search 和 RRF 合并。真实模型运行需要安装 `FlagEmbedding`，本地 Python 3.12 wrapper 会安装 BGE-M3 推理依赖，并以 `--no-deps` 安装 `FlagEmbedding`，避免 `ir-datasets -> zlib-state` 在 macOS arm64 上的源码编译问题。

验收记录（2026-06-11）：

- `pska_smoke` 执行 `db-reset` 后应用 `002_bge_m3_embeddings.sql`，`chunks.embedding` 为 `vector(1024)`，pgvector 可用。
- fresh import `~/Downloads/twitter_archive` 3 个 zip 时启用 `--embedding-provider bge-m3`，6/6 个 chunk 自动写入 `bge-m3 / BAAI/bge-m3 / 1024` embedding。
- `embed-backfill --embedding-provider bge-m3` 对已嵌入数据幂等，重复运行 `embedded=0, failed=0`。
- 概念查询在 `lexical_candidates=0` 时仍通过 vector 召回，`score_debug` 显示 `ranker=hybrid_rrf`、`vector_enabled=true`、`vector_candidates=6`。
- 单元测试覆盖导入时嵌入、backfill 幂等和 vector-only retrieval debug；core `36 passed`，Twitter/X channel `9 passed`。

要做：

- 增加 embedding provider 配置，支持本地配置文件和环境变量。
- 为 `chunks.embedding` 写入真实向量。
- 增加批量 backfill/reindex 命令。
- 搜索时使用 FTS + vector + RRF 合并。
- 报告里显示 lexical/vector/combined 分数。

依赖：

- 现有 `chunks.embedding vector(1024)` schema。
- LLM/API key 配置读取机制。

验收：

- 新导入文档自动生成 embedding。
- 老数据可通过命令 backfill。
- 概念查询不含精确关键词时也能召回相关 chunk。
- E2E 报告显示 vector search 生效，而不是 `semantic_placeholder`。

### P0.2 异步 job system（第一版已实现）

目的：导入 zip、LLM 提取、embedding、报告生成都不应长期阻塞 CLI/API 请求。

当前状态：已实现第一版 durable local job system。`jobs` / `job_events` 持久化任务和事件，CLI 提供 `job-submit`、`job-run`、`job-status`、`job-retry`、`job-recover`、`job-worker`，HTTP API 提供 `/jobs`、`/jobs/run`、`/jobs/recover`、`/jobs/{id}`、`/jobs/{id}/retry`。import、extract、embed、full report 均可作为 job 执行。`job-run --until-empty` 可一次性清空当前队列，`job-worker` 可常驻轮询执行；stale running job 可按 max age 恢复为 queued 或 failed。下一步可把 worker 接入 launchd/systemd 或 UI 控制台。

验收记录（2026-06-11）：

- `003_jobs.sql` 在 `pska_smoke` 上创建 `jobs` / `job_events`。
- `job-submit extract_all` 可创建 queued job。
- `job-run --limit 1` 可 claim 并执行 queued job，状态变为 `succeeded`，attempts 递增。
- `job-run --until-empty` 可 drain 队列，`job-worker` 可按 poll interval 持续消费任务。
- `job-recover` 可恢复中断后遗留的 stale running job，未超过 attempts 时重新排队，超过后标记 failed。
- 单元测试覆盖 import/embed/extract job 重跑幂等：不会重复写入 source/document/chunk/entity/hyperedge/review item。
- `job-status --job-id ...` 可查看 job 详情和事件时间线：queued、started、execute、succeeded。
- 单元测试覆盖 job 成功、失败、手动 retry、full report job 执行器和 CLI parser；core `48 passed`，Twitter/X channel `9 passed`。

要做：

- 新增 `jobs` / `job_events` 表或等价任务存储。
- 将 import、extract、embed、report 拆成可重试 job。
- 每个 job 记录 status、started_at、finished_at、error、attempts。
- CLI/API 能查看 job 状态。
- 失败任务可重试，不重复写入 source/document/chunk/entity/hyperedge。

依赖：

- 当前 import idempotency。
- LLM extraction schema repair telemetry。

验收：

- 中断一次 extraction 后可恢复。
- 单个 source LLM 失败不会让整个导入批次不可用。
- 报告能展示 job timeline 和失败原因。

### P0.3 Review / approval 工作流

目的：共享、敏感记忆、profile 更新、实体合并和删除不能直接落库生效。

要做：

- 完善 review item 状态机：pending、approved、rejected、applied、expired。
- 增加 apply/reject decision API 和 CLI。
- team visibility 变更必须走 review。
- 高敏感 profile/memory 更新必须走 review。
- 每个决策写入 audit event。

依赖：

- 当前 `review_items` 和 `audit_events` 表。
- 用户/team/visibility 模型。

验收：

- LLM 提出共享建议时只生成 review，不直接共享。
- approval 后才改变 visibility 或写入 profile。
- reject 后不改变知识对象。
- HTML 报告展示 pending/approved/rejected review。

### P0.4 Full report 改进为正式验收套件

目的：把 `twitter_full_report.py` 从调试脚本升级成长期验收工具。

要做：

- 报告里明确显示 PSKA direct、MCP direct、Fastreact full Agent 的技术路径和差异。
- Fastreact event stream 展示 tool call、tool result、final answer。
- 报告自动标出 LLM JSON repair、schema repair、失败步骤和耗时瓶颈。
- 增加历史报告目录和 run id。
- 支持只重跑某一阶段，如 `--skip-import`、`--only-fastreact`。

依赖：

- 当前 `core/scripts/twitter_full_report.py`。

验收：

- 一次完整报告可以复盘所有关键路径。
- 任一步失败仍生成 HTML/JSON。
- 报告不泄露 API key/home path。

## P1: 让系统开始可日常使用

### P1.1 Conversation ingestion

目的：用户长期对话也应成为知识来源，不只依赖文件和 Twitter/X。

要做：

- 定义 conversation/message channel payload。
- 支持会话、消息、参与者、时间、工具调用、引用来源。
- 对话进入 source_items/documents/chunks。
- LLM 提取 memory/profile/hyperedge 时保留 message provenance。

验收：

- 一段对话可导入为 source。
- 用户偏好、项目事实、待办可以从对话中生成 review item。
- 回答能引用具体 conversation message。

### P1.2 Profile card 与 agent memory 生命周期

目的：让 Agent 对用户的长期理解可见、可改、可遗忘。

要做：

- Profile Agent 生成 profile card update proposal。
- Memory Agent 管理 working/episodic/semantic/procedural/profile 分层。
- 增加 memory confidence、last_verified_at、decay_policy 更新逻辑。
- 用户可以 approve/reject/forget memory。

验收：

- Agent 不能直接把高敏感事实写入 profile。
- 所有 profile 更新有 source_refs。
- 低置信/过期 memory 不自动提升为 profile。

### P1.3 Retrieval quality upgrade

目的：让 RAG 和 GraphRAG 在真实问题上更稳。

要做：

- 加入 reranker。
- 支持 query rewrite 和多轮检索策略。
- 支持 graph-global 查询，而不是只靠 chunk 命中触发一跳图扩展。
- 增加 conflict search。
- 文件/URL/标题精确查找与 RAG 分流。

验收：

- “列出最重要实体和关系”不再因为 chunk 检索 miss 而失败。
- exact URL/title/person 查询稳定命中。
- 概念查询能召回跨文档证据。

### P1.4 Local management UI

目的：没有 UI，PSKA 很难长期使用和维护。

要做：

- 本地控制台显示 source、documents、chunks、entities、hyperedges。
- Review queue 页面支持 approve/reject。
- Profile/memory 页面支持查看、编辑、遗忘。
- Search/QA 页面显示 answer、citations、graph context。
- System health 页面显示 jobs、LLM repair、失败任务、索引状态。

验收：

- 用户无需看数据库即可理解系统状态。
- 可在 UI 中完成 review 决策。
- 可从答案跳回 source/document/chunk/artifact。

### P1.5 Channel ingestion framework

目的：未来不止 Twitter/X，要接文件、网页、浏览器剪藏、对话等来源。

要做：

- 抽象 channel payload validation。
- 每个 channel 有 schema version、source_channel、artifact policy。
- 插件/CLI 统一使用 `pska.archive.v2` 或 channel payload。
- 增加 channel registry 和 importer registry。

验收：

- 新增一个 channel 不需要改核心 ingest 流程。
- source_channel 可以驱动不同 extraction prompt 和 artifact 展示。

## P2: 生产化和高级智能

### P2.1 Audit coverage

目的：所有写入、共享、删除、profile/memory 变更都可追溯。

要做：

- 所有 mutating API 写 audit_events。
- 报告展示最近 audit 摘要。
- 支持按用户/source/review item 查询审计。

验收：

- 任一重要状态变化都能追到 actor、time、before/after、reason。

### P2.2 Backup / export / restore

目的：家庭服务器必须能备份和恢复。

要做：

- PostgreSQL dump 脚本。
- Archive artifact 校验。
- JSON export for selected user/team/space。
- Restore smoke。

验收：

- 新机器可从 backup 恢复 pska_smoke/pska。
- artifact hash 校验能发现丢失文件。

### P2.3 Multi-user / team hardening

目的：多用户和 team 不是只在 schema 里存在，要实际可靠。

要做：

- 用户切换和 represented_user_id 测试。
- team membership 管理。
- agent_service 不能绕过 ACL。
- team-visible 数据泄露测试。

验收：

- 私有内容只对 owner/admin 可见。
- team 内容只对 selected teams 可见。
- hypergraph expansion 不泄露 private/team-restricted 节点。

### P2.4 HippoRAG / GraphRAG v2

目的：在轻量超图基础上探索更强的图检索。

要做：

- 在现有 entities/hyperedges 上增加 graph algorithms。
- 评估 PPR、recognition memory、open KG merge。
- 保持 source-grounded，不能让图谱变成无证据幻觉层。

验收：

- 图检索能改善多跳问题。
- 每个图结论仍可追溯到 source_refs。

## 当前风险清单

- LLM 抽取质量不稳定：需要更强 schema constraints、prompt versioning 和 eval dataset。
- 检索仍偏弱：embedding/rerank 未完成前，复杂问题会 miss。
- 同步 LLM 调用太慢：必须 job 化和缓存。
- Review 未产品化：共享/记忆/profile 仍缺少可操作界面。
- Fastreact full Agent 依赖模型配置：报告已修复 key/model/base_url 注入，但长期应有 config health check。
- HTML 报告很有用，但不是 UI：不能替代日常管理控制台。

## 建议执行顺序

1. P0.1 embedding pipeline。
2. P0.2 job system。
3. P0.3 review approval。
4. P1.3 retrieval quality upgrade。
5. P1.1 conversation ingestion。
6. P1.2 profile/memory lifecycle。
7. P1.4 local UI。

理由：先解决“搜得准”和“跑得稳”，再解决“记得住”和“管得好”，最后做 UI 承载日常使用。
