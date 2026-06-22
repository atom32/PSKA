# PSKA Roadmap / TODO

归档历史路线图。本文记录旧 milestone 和阶段性判断，不再作为当前任务选择入口。当前入口见
[中文文档索引](../../README.zh.md)、[Product Design](../../../core/docs/product-design-zh.md)
和 [Architecture Status](../../../core/docs/architecture-status-zh.md)。

日期：2026-06-12

长期愿景见 [vision-zh.md](../../../core/docs/vision-zh.md)。

## 系统化推进入口

PSKA 后续不再靠用户不断说“继续，下一个 TODO”来推进。当前 repo 文档中的源头分工如下：

- [product-design-zh.md](../../../core/docs/product-design-zh.md)：产品定位、人类日常工作流、PSKA/FastReAct 边界和产品原则。
- [architecture-status-zh.md](../../../core/docs/architecture-status-zh.md)：模块级设计目标、实现状态、成熟度、缺口和 open-source-first 候选。
- [todo-implement-system-zh.md](todo-implement-system-zh.md)：结构化 TODO、任务选择规则、验收门禁和当前 Human Workflow backlog。
- [mvp-user-scope-zh.md](mvp-user-scope-zh.md)：MVP+ 数据源和功能 scope。

当前 **Human Workflow** 第一轮已完成：`HW-001` 到 `HW-008` 均已实现并验证；**Admin Console** 第一轮 `UI-001` 到 `UI-006` 也已实现并验证。下一轮 backlog 已在 [todo-implement-system-zh.md](todo-implement-system-zh.md) 生成；后端稳定化仍可从 `HW-009` 等任务继续，产品主入口应从 `APP-001` User Workspace Skeleton 开始。

更新：2026-06-14

- P0.1 service contract 已落地：HTTP `/mcp`、稳定 `/ready`、service contract 文档。
- P0.2 auth/request context 已落地：service token、agent_service、represented user、HTTP/MCP ACL context。
- P0.3 job worker metadata 已落地：worker lease、heartbeat、Fastreact `external_run_id`、source refs。
- P0.5 observability/operations 已部分落地：增强 `/ready`、`service-check`、foreground `local-daemon` supervisor、runbook、job stats/list/filter/cancel/retry/recover ops API 和 CLI，`digest_backlog`、embedding coverage、source-channel freshness 指标，HTTP request id 与结构化访问日志。
- P0.4 background digest loop 已完成 write-back + external worker lifecycle + backlog scheduling + foreground periodic scheduler + Fastreact worker slice：`pska_job_context`、`pska_write_candidates`、`POST /candidates`、`POST /jobs/{id}/lease`、`GET /digest/batches/{id}`、`POST /digest/candidates`、`POST /digest/schedule`、CLI `digest-scheduler`、`complete/fail`、priority、retry backoff、candidate schema version、batch cursor、Fastreact `pska_digest` worker 脚本。仍待补更细的 candidate taxonomy、daemon supervisor 化和真实 digest 质量调优。
- P1.1 connector contract 已完成第一版：`pska.connector_record.v1`、`POST /connectors/records`、CLI `connector-ingest-record`，以及 `pska.connector_state.v1`、`GET/POST /connectors/states`、CLI `connector-state`。具体 Files/Browser/Git connector 实现仍待 P1.2+。
- P2 retrieval quality 已补 GraphRAG grounding 第一版：hypergraph context 返回 ACL-filtered `source_refs` 和 `evidence_citations`，并能从 query seed entity 返回最多 2-hop 的 grounded `graph_paths`；实体链接支持 metadata aliases/canonical label/slug/handle 的轻量匹配，并可选用 `rapidfuzz` 处理长 alias typo；graph paths 已有基于 query mention、confidence、evidence coverage、path length 的第一版排序和 explanation；chunk retrieval 已有 recency/source authority tie-breaker，并可选用 `rank-bm25` 做 lexical scorer；retrieval diagnostics 已能输出 insufficient/ungrounded evidence、graph conflict、sensitivity flags；profile/memory context 已能带 source refs/citations 进入 retrieval response。当前仍是轻量路径扩展，不是 GNN，也不是 HippoRAG/PPR 级别的成熟 GraphRAG。
- P4 Admin Console 已完成第一版：`/console`、`/console/reviews`、`/console/search`、`/console/memory`、`/console/jobs` 和 `/console/sources` 已通过测试和本地 HTTP smoke。这个成果证明 PSKA service、Postgres、service token、HTTP API 和管理入口可用；但它仍是管理台，不是最终用户的 chat/writer 产品。

## 当前判断

PSKA 已完成初期 MVP 闭环：

```text
Twitter/X 归档 -> PostgreSQL + pgvector schema -> LLM 提取 -> 轻量超图
-> ACL-first retrieval -> agentic QA -> CLI / HTTP API / stdio MCP
-> FastReAct 通过服务边界调用 PSKA
```

这证明了 PSKA 可以作为私有优先知识库运行，但还不是长期愿景里的完整 online personal context service。MVP 阶段应先收窄数据源到 Twitter/X archive 和本地文本文件，把主线从“继续扩 connector”转向“稳定服务化、分析闭环和用户工作台”：

```text
online service daemon -> background jobs -> idle digest -> review/memory -> reliable retrieval/QA
```

## 设计边界

- PSKA 是个人知识基础设施和机制层，拥有知识存储、ACL、connector、digest job、memory graph、review、jobs、citations 和 audit。
- FastReAct 是 agentic 公用服务层，负责所有 agentic loop 行为：planning、工具编排、模型调用、session runtime、事件流和多步任务执行。
- FastReAct 或其他 agentic layer 只能通过 PSKA HTTP API / MCP tools 使用 PSKA 能力，不能直接访问 PSKA DB，不能绕过 PSKA ACL。
- Agent service 身份必须带 represented user / service identity / scope 访问 PSKA，权限判断由 PSKA 统一执行。
- 长任务必须任务化，不能阻塞在线请求。
- 所有 digest、memory 和主动建议都必须保留 source refs。
- 未来其他系统接入 PSKA 时，也应复用同一套 API/MCP 契约，而不是依赖 FastReAct 或 PSKA 的内部实现。
- Open-source-first：非 PSKA 核心边界能力优先采用成熟开源项目或库，避免从头造轮子。PSKA 自己实现的重点应限于私有权限、source refs、review/audit、canonical data model、service contract 和与 FastReAct 的边界。检索、解析、watch、rerank、评测、daemon 包装、UI 组件等应先评估现成方案。

## P0: Online Service Foundation

目标：让 PSKA 从本地 CLI/MCP 工具变成可常驻、可健康检查、可被多个 client 依赖的 online service。

### P0.1 Service contract

- 明确 PSKA online API：
  - `GET /health`
  - `GET /ready`
  - `GET /index-status`
  - `POST /search`
  - `POST /agentic-search`
  - `POST /ingest/channel-payload`
  - `POST /jobs`
  - `GET /jobs/{job_id}`
  - `GET /review-items`
  - `POST /review/{id}/approve`
  - `POST /review/{id}/reject`
- 为 FastReAct / 其他 agent 提供 HTTP MCP endpoint：
  - `POST /mcp`
  - JSON-RPC `initialize`
  - JSON-RPC `tools/list`
  - JSON-RPC `tools/call`
- 产出 OpenAPI 或等价 contract 文档。

验收：

- service 可常驻启动。
- `/ready` 能报告 DB、migration、embedding、LLM、worker、index counts。
- FastReAct 可通过 `transport=http` 使用 PSKA MCP。

### P0.2 Auth and ACL request context

- 定义请求身份：
  - user token
  - service token
  - agent_service caller
  - represented_user_id
- API、MCP、job worker 使用统一 request context。
- 所有 search、agentic-search、digest、review 查询都走 ACL-first。
- source item 继承 connector 授权范围和 visibility。

验收：

- agent_service 没有 represented_user_id 时不能读私有知识。
- represented_user_id 只能读该用户本来可见的内容。
- public/private/team 三类 source item 有覆盖测试。

### P0.3 Job system and workers

- 新增或完善 `jobs` / `job_events` 表。
- 将这些操作任务化：
  - import
  - extract
  - embed
  - digest
  - report
  - review apply
- job 记录 status、started_at、finished_at、error、attempts、source refs。
- 支持 retry、recover stale、until-empty worker。
- 幂等写入 source/document/chunk/entity/hyperedge/memory。

验收：

- 中断 extraction 后可恢复。
- 单个 source LLM 失败不会让整个导入批次不可用。
- API 提交 job 后立即返回 job id。

### P0.4 Background digest loop

目标：PSKA 提供 digest 机制、任务状态和受控工具；FastReAct worker 负责执行具体 agentic digest loop。

PSKA 侧：

- `jobs` 类型支持 `digest_via_fastreact`。已完成。
- `POST /jobs` 可创建 digest job，支持 scope、source refs 和 priority。已完成。
- `GET /jobs/{job_id}/context` 和 MCP `pska_job_context` 返回 scoped source/chunk context。已完成第一版。
- `POST /candidates` 和 MCP `pska_write_candidates` 写入 entity、relationship、review、memory/profile candidates。已完成第一版。
- `POST /jobs/{job_id}/lease` 领取任务，返回 scoped context 和 allowed tools。已完成第一版。
- `GET /digest/batches/{job_id}` 返回待处理 source/chunk/context，并经过 owner scope 过滤。已完成第一版。
- `POST /digest/candidates` 写入 entity、relationship、review、memory/profile candidates。已完成第一版。
- `POST /review-items` 写入需要用户确认的摘要、提醒或 action candidate。部分由 `POST /candidates` 的 `review_items` 覆盖。
- `GET /digest/batches/{job_id}?cursor=...&limit=...` 支持 batch cursor。已完成第一版。
- `POST /jobs/{job_id}/complete` 和 `POST /jobs/{job_id}/fail` 支持完成、失败、retry backoff。已完成第一版。
- `POST /digest/schedule` 和 CLI `digest-schedule` 从 source backlog 幂等创建 `digest_via_fastreact` job。已完成第一版。
- CLI `digest-scheduler` 周期性调用 backlog scheduler，支持 max cycles、idle limit、stale recovery 和 backlog 上限。已完成 foreground 第一版。
- 所有候选结果必须带 source refs、confidence、producer、schema version、request id 和 audit event。schema version 已支持 `pska.candidates.v1`。

FastReAct 侧：

- 增加 PSKA digest worker skill/workflow。脚本型 worker 第一版已完成。
- worker 从 PSKA lease job，不从 PSKA DB 取数据。已完成第一版。
- worker 调用 PSKA tools 获取上下文、执行 LLM 推理、写回 candidates/review items。
- worker 输出 FastReAct agent event stream，PSKA 只保存必要 audit 和结果引用。
- worker 失败时返回结构化错误，PSKA 决定 retry、降级或进入 review。

策略：

- 新数据优先。backlog scheduler 第一版按 source 创建时间倒序选取，跳过已排队/运行/成功的来源；foreground scheduler 可周期性触发。
- 低成本轻 digest 先行，高成本 LLM digest 延后。
- retry backoff 已由 PSKA 控制；quota window 已完成第一版：`digest-schedule`/`digest-scheduler` 支持 `quota_window_seconds` 和 `max_jobs_per_window`，限制自动 digest job 创建频率；idle window 仍待结合 daemon/usage pattern 调优。
- 高影响 action 默认进入 review，不自动执行。

验收：

- 导入数据后 worker 能自动生成 digest candidates。
- digest candidates 都带 source refs。
- 低置信或高影响内容进入 review。
- 停掉 FastReAct 后 PSKA service 仍能启动、检索和管理 job backlog。
- 换成其他 agentic executor 时，只要实现同一套 job/tool contract，就能继续消费 PSKA digest job。

### P0.5 Observability and operations

- API 日志包含 request id、job id、source ref count。已完成第一版；worker 日志和 Fastreact trace 对齐继续由 Fastreact worker 侧完善。
- 提供 basic metrics：
  - index counts
  - pending/running/failed jobs 已完成第一版
  - digest backlog 已完成第一版
  - embedding coverage 已完成第一版
  - source-channel freshness 已完成第一版；真正 last successful connector scan 留给 P1 connector state
- 提供 manual：
  - 本地启动
  - service 启动
  - worker 启动
  - FastReAct HTTP MCP 配置
  - 常见故障
- Job ops 第一版已完成：
  - `GET /jobs?status=&job_type=&limit=`
  - `GET /jobs/stats`
  - `POST /jobs/{job_id}/cancel`
  - `POST /jobs/{job_id}/retry`
  - `POST /jobs/recover-stale`
  - CLI `jobs list|stats|show`、`job-cancel`、`job-retry`、`job-recover`

验收：

- 用户能用一组命令启动 PSKA service + worker。
- 失败时能从 `/ready` 和 job status 定位问题。

已完成第一版：

- `./scripts/pska local-daemon` 前台 supervisor 可同时启动 service、job worker、digest scheduler。

## P1: Connector Architecture

目标：把 Twitter/X 从一个特例变成 connector 体系中的一个实现。

### P1.1 Connector contract

- 定义 connector 输出：
  - connector_id 已完成第一版
  - external_id 已完成第一版
  - source_uri 已完成第一版
  - title 已完成第一版
  - body / artifact refs 已完成第一版
  - timestamps 已完成第一版
  - owner_user_id 已完成第一版
  - visibility 已完成第一版
  - permission metadata 已完成第一版
  - content hash 已完成第一版
- 支持 scan cursor 和增量同步。source-level scan_cursor 已保留；durable connector cursor state 已完成第一版。
- 支持 connector-level enable/disable 和授权范围。record-level permission metadata 已保留；connector state 的 enabled、permission_scope、config 已完成第一版。

### P1.2 Files connector

文件是 PSKA 最基础的管理目标。

- 本机目录授权。已完成第一版：`files-scan --root` 会把 root 写入 connector state `permission_scope.roots`。
- 文件 metadata 扫描。已完成第一版：path、mime type、size、mtime、source URI。
- 文本抽取。已完成第一版：UTF-8 文本类文件；可选 `pska-core[documents]` 用 `pypdf`/`python-docx` 抽取 PDF/DOCX 文本。
- content hash 去重。已完成第一版：connector record 携带 `sha256`，PSKA ingest 仍按内容 hash 幂等。
- 持续监听。已完成第一版：可选 `pska-core[watch]` + `files-watch` 用 `watchdog` 前台监听授权 root 并触发同一套 sync。
- 文件移动/改名检测。已完成第一版：`connector_state.config.files_manifest` 记录 lightweight manifest，`files-sync`/`files-scan` 可区分 new、changed、unchanged、moved、missing；missing 不删除 canonical source history。
- ignore rules。已完成第一版：默认忽略 `.git`、`__pycache__`、`.DS_Store`，CLI 支持 `--ignore`。

### P1.3 Browser and web connector

- 当前页面保存。
- 书签/阅读列表导入。
- 网页正文抽取。
- source refs 指向 URL 和 capture time。

### P1.4 Git repo connector

- repo metadata、commit、branch、tag。
- README/docs/issues/PR 本地或远端摘要。
- 项目实体和时间线。

### P1.5 Mail, photos, NAS, Home Assistant, conversations

按授权范围逐步接入，先 contract 后实现。

## P2: Memory Model and Retrieval Quality

- 强化 people/project/place/event/timeline schema。
- 将 digest candidates 转为 reviewable memory。
- 改进 hybrid retrieval：
  - exact source
  - lexical
  - vector
  - graph one-hop 已完成第一版
  - graph evidence grounding 已完成第一版
  - graph path expansion 已完成第一版
  - entity alias linking 已完成第一版
  - graph path ranking/explanation 已完成第一版
  - recency 已完成第一版
  - source authority 已完成第一版
- 明确 gap/conflict/sensitivity 输出。已完成规则诊断第一版；后续仍需要 LLM/用户 review 辅助判断。
- 支持 profile context 和 memory context 的可解释引用。已完成第一版。

## P3: Proactive Agentic Service

目标：PSKA 不只被动回答，也能主动服务。

- 每日/每周 briefing。
- 项目雷达。
- 文件整理建议。
- 邮件/对话待办候选。
- 照片/事件 digest。
- 关系和上下文提醒。
- 主动事件统一进入 notification/review channel。

验收：

- 主动建议不直接执行高影响动作。
- 每条建议可追溯 source refs。
- 用户可以 approve/reject/snooze。

## P4: Product UI

已完成 Admin Console 第一轮：

- Home dashboard。
- Review console。
- Source/connector 管理。
- Job dashboard。
- Search and citation viewer。
- Memory/profile read-only view。

下一轮 Product UI 不应继续堆管理 API，而应转向 User Workspace：

- Chat Workspace：把 search/agentic search 变成对话主流程，答案以中文为默认，并展示 citations、graph evidence、gaps/conflicts、memory/profile 使用说明。
- Corpus / Wiki Explorer：让用户看懂 source/document/chunk/entity/hyperedge/memory/profile 的内容、关系和出处。
- Writer Mode：富文本创作、选中文本、基于 PSKA 数据的写作建议。
- Evidence Inspector：统一展开任意回答、建议、记忆、profile、graph edge 的 source refs、证据片段、置信度和 review 状态。
- Retrieval Quality Loop：把真实问题、expected citations、graph path relevance 和失败案例纳入回放，而不是只做主观 demo。

## FastReAct Integration Track

FastReAct 仍然是重要消费者，但不应主导 PSKA 架构。

近期目标：

- 保持当前 stdio MCP 可用。
- 增加 PSKA HTTP MCP endpoint 后，让 FastReAct 通过 `transport=http` 使用 PSKA。
- FastReAct service auth 与 PSKA service auth 分离。
- FastReAct 不直接访问 PSKA DB。

相关文档：

- [fastreact-protocol-zh.md](../../../core/docs/fastreact-protocol-zh.md)
- [fastreact-pska-real-integration-manual-zh.md](../../../core/docs/fastreact-pska-real-integration-manual-zh.md)

## 当前优先级建议

截至当前，P0.1/P0.2/P0.3/P0.4/P0.5 的核心机制都已有第一版，P1.2 Files connector 也已经覆盖 config sync、watch、PDF/DOCX optional extraction、manifest reconciliation 和缺失文件记录。下一步不再是补服务骨架，而是把 PSKA 从“机制可跑”推进到“人类日常可用”。

结构化任务以 [todo-implement-system-zh.md](todo-implement-system-zh.md) 为准。Human Workflow 第一轮已完成并验证：

1. `HW-001` Daily Status Entry。
2. `HW-002` Review Summary。
3. `HW-003` Memory/Profile Read-only View。
4. `HW-004` Deterministic Daily Briefing v0。
5. `HW-005` FastReAct Narrative Briefing。
6. `HW-006` Agent Conversation Capture。
7. `HW-007` Grounded Graph Candidate Review。
8. `HW-008` Digest Budget Policy。

下一轮结构化 backlog 已生成在 [todo-implement-system-zh.md](todo-implement-system-zh.md)。后端稳定化 ready 起点仍是 `HW-009` Digest E2E Write-back Gate、`HW-010` Review Batch Operations、`HW-013` Human-readable Ops Briefing 和 `HW-014` Local Daemon Productization；产品入口的 ready 起点已从 Admin Console 转为 `APP-001` User Workspace Skeleton。

历史优先顺序和技术线索保留如下：

1. **真实 digest E2E 稳定化**
   - Fastreact digest worker 修复 tool budget/重复写回问题。
   - 用当前 `postgresql:///pska` 的有限 docs/Twitter 样本跑通：lease job -> batch context -> LLM digest -> candidates/review/memory/profile 写回 -> complete job。
   - 加一个只读/半自动 gate，验证 digest job 不只是 queued，而是真的产生 grounded candidates。
   - 优先复用：FastReAct 现有 worker/skill/event stream，PSKA 只补 contract/gate；不要在 PSKA 内重写 agent loop。

2. **Review taxonomy 和候选质量**
   - 明确 `review_items` 类型：memory_candidate、profile_update、relationship_candidate、action_candidate、conflict、low_confidence。已完成第一版：enum/schema/migration 已支持这些类型。
   - 高影响/低置信候选进入 review；低风险摘要或关系候选可批量 approve。已完成第一版：低置信 memory/relationship candidate 会进入 review，不直接写 memory/graph。
   - Review apply 后必须写 audit event，并保留 source refs。
   - 优先复用：Pydantic/JSON Schema 类 schema validation、现成 diff/approval UI 组件；PSKA 自己保留 audit/source-ref/write boundary。

3. **Memory promotion lifecycle**
   - 将 digest candidates 稳定转成 agent memories/profile cards/hyperedges。
   - 增加 memory confidence、decay、last_verified_at 的实际更新路径。
   - 做 `memory-list`/`memory-review`/`profile-list` 这类人类可检查 CLI。
   - 优先复用：现有知识图谱/实体归一化工具、dateparser/rapidfuzz 等轻量库；PSKA 自己定义 memory lifecycle 和 ACL。

4. **Retrieval/GraphRAG 质量打磨**
   - 当前是轻量 GraphRAG，不是 GNN/HippoRAG/PPR。
   - 下一步优先做 rerank/evaluation，而不是上 GNN：加入 query fixture、expected citations、graph path relevance、conflict/gap regression。
   - 如果真实问题经常需要跨多文档多跳，再评估 HippoRAG/PPR 层。
   - 优先复用：`rank-bm25` 和 `rapidfuzz` 已作为可选 retrieval extras 接入；下一步评估 pgvector/Postgres FTS、Tantivy/LanceDB/Qdrant，rerank 可评估 sentence-transformers/cross-encoder，GraphRAG 可评估 HippoRAG/PPR 而非自研 GNN。

5. **Service daemon 产品化**
   - `local-daemon` 仍是 foreground supervisor。
   - 后续补 launchd/systemd wrapper、日志路径、pid/status、restart policy、配置检查。
   - `/ready` 增加 Fastreact worker/digest backlog 质量信号，而不是只看服务是否在线。
   - 优先复用：launchd/systemd/supervisord 或 honcho/foreman 类 process manager；PSKA 只生成配置和健康检查。

6. **Human-facing MVP workflow**
   - 固化日常命令：`files-sync`/`files-watch`、`digest-schedule`、`mvp-status --summary`、`review-list`、`agentic-search`。
   - 做一个 `daily-briefing` 或 `inbox` 第一版，把 digest/review/memory 串成用户每天能看的输出。
   - 优先复用：Typer/Rich/Textual 或轻量 Web UI 组件库；先做可用 workflow，不从零做完整产品框架。

7. **Connector 扩展保持克制**
   - MVP 阶段继续只把 Twitter/X archive 和 local files 做扎实。
   - Browser/Git connector 可以作为下一轮 P1.3/P1.4，但不应早于 digest/review/memory 闭环稳定。
   - Mail/photos/NAS/Home Assistant/conversations 继续后置。
   - 优先复用：readability-lxml/trafilatura/Playwright、GitPython/PyDriller、Unstructured/Docling/Marker、watchdog 等成熟 connector/解析库；PSKA connector 只负责授权、manifest、source refs 和 canonical ingest。
