# PSKA Roadmap / TODO

日期：2026-06-12

长期愿景见 [vision-zh.md](vision-zh.md)。

更新：2026-06-14

- P0.1 service contract 已落地：HTTP `/mcp`、稳定 `/ready`、service contract 文档。
- P0.2 auth/request context 已落地：service token、agent_service、represented user、HTTP/MCP ACL context。
- P0.3 job worker metadata 已落地：worker lease、heartbeat、Fastreact `external_run_id`、source refs。
- P0.5 observability/operations 已部分落地：增强 `/ready`、`service-check`、foreground runbook、job stats/list/filter/cancel/retry/recover ops API 和 CLI，`digest_backlog`、embedding coverage、source-channel freshness 指标，HTTP request id 与结构化访问日志。
- P0.4 background digest loop 已完成 write-back + external worker lifecycle + backlog scheduling + foreground periodic scheduler + Fastreact worker slice：`pska_job_context`、`pska_write_candidates`、`POST /candidates`、`POST /jobs/{id}/lease`、`GET /digest/batches/{id}`、`POST /digest/candidates`、`POST /digest/schedule`、CLI `digest-scheduler`、`complete/fail`、priority、retry backoff、candidate schema version、batch cursor、Fastreact `pska_digest` worker 脚本。仍待补更细的 candidate taxonomy、daemon supervisor 化和真实 digest 质量调优。
- P1.1 connector contract 已完成第一版：`pska.connector_record.v1`、`POST /connectors/records`、CLI `connector-ingest-record`，以及 `pska.connector_state.v1`、`GET/POST /connectors/states`、CLI `connector-state`。具体 Files/Browser/Git connector 实现仍待 P1.2+。
- P2 retrieval quality 已补 GraphRAG grounding 第一版：hypergraph context 返回 ACL-filtered `source_refs` 和 `evidence_citations`，并能从 query seed entity 返回最多 2-hop 的 grounded `graph_paths`；实体链接支持 metadata aliases/canonical label/slug/handle 的轻量匹配；graph paths 已有基于 query mention、confidence、evidence coverage、path length 的第一版排序和 explanation；chunk retrieval 已有 recency/source authority tie-breaker；retrieval diagnostics 已能输出 insufficient/ungrounded evidence、graph conflict、sensitivity flags；profile/memory context 已能带 source refs/citations 进入 retrieval response。当前仍是轻量路径扩展，不是 GNN，也不是 HippoRAG/PPR 级别的成熟 GraphRAG。

## 当前判断

PSKA 已完成初期 MVP 闭环：

```text
Twitter/X 归档 -> PostgreSQL + pgvector schema -> LLM 提取 -> 轻量超图
-> ACL-first retrieval -> agentic QA -> CLI / HTTP API / stdio MCP
-> FastReAct 通过服务边界调用 PSKA
```

这证明了 PSKA 可以作为私有优先知识库运行，但还不是长期愿景里的 online personal context service。下一阶段主线应该从“单次导入和问答”转向：

```text
online service -> background jobs -> idle digest -> multi-source connectors -> proactive agentic service
```

## 设计边界

- PSKA 是个人知识基础设施和机制层，拥有知识存储、ACL、connector、digest job、memory graph、review、jobs、citations 和 audit。
- FastReAct 是 agentic 公用服务层，负责所有 agentic loop 行为：planning、工具编排、模型调用、session runtime、事件流和多步任务执行。
- FastReAct 或其他 agentic layer 只能通过 PSKA HTTP API / MCP tools 使用 PSKA 能力，不能直接访问 PSKA DB，不能绕过 PSKA ACL。
- Agent service 身份必须带 represented user / service identity / scope 访问 PSKA，权限判断由 PSKA 统一执行。
- 长任务必须任务化，不能阻塞在线请求。
- 所有 digest、memory 和主动建议都必须保留 source refs。
- 未来其他系统接入 PSKA 时，也应复用同一套 API/MCP 契约，而不是依赖 FastReAct 或 PSKA 的内部实现。

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
- retry backoff 已由 PSKA 控制；quota 和 idle window 待补。
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

- 本机目录授权。
- 文件 metadata 扫描。
- 文本抽取。
- content hash 去重。
- 文件移动/改名检测。
- ignore rules。

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

- Review console。
- Source/connector 管理。
- Job dashboard。
- Search and citation viewer。
- Memory graph explorer。
- Proactive briefing inbox。

## FastReAct Integration Track

FastReAct 仍然是重要消费者，但不应主导 PSKA 架构。

近期目标：

- 保持当前 stdio MCP 可用。
- 增加 PSKA HTTP MCP endpoint 后，让 FastReAct 通过 `transport=http` 使用 PSKA。
- FastReAct service auth 与 PSKA service auth 分离。
- FastReAct 不直接访问 PSKA DB。

相关文档：

- [fastreact-protocol-zh.md](fastreact-protocol-zh.md)
- [fastreact-pska-real-integration-manual-zh.md](fastreact-pska-real-integration-manual-zh.md)

## 当前优先级建议

下一步优先做：

1. P0.1 Service contract。
2. P0.2 Auth and ACL request context。
3. P0.3 Job system and workers。
4. P0.4 Background digest loop。

这些完成后，PSKA 才真正从“可用 MVP”进入“可常驻、可主动、可扩展的数据底座”。
