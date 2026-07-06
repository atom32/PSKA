# PSKA / Fastreact Agentic Boundary

日期：2026-06-27

PSKA 采用 Postgres-first knowledge core + Fastreact agentic service 的双系统边界。

## 职责

PSKA 负责：

- source、document、chunk、embedding、entity、hyperedge、memory、profile、review、audit、jobs
- ACL 和 represented user 权限判断
- citations、source refs 和知识写回边界
- HTTP API 和 MCP tools

Fastreact 负责：

- LLM 调用
- agent planning
- tool orchestration
- session/run lifecycle
- SSE event stream
- tool approval 和 trace

Fastreact 不能直接访问 PSKA DB。PSKA 不 import Fastreact Python internals。两者只通过 HTTP/SSE 和 MCP 工具协议通信。

## 统一问答入口

产品入口统一为 **Ask PSKA**：

- 主 API：`POST /workspace/ask`
- 流式 API：`POST /workspace/ask/stream`
- 兼容/诊断 API：`POST /workspace/search/query`、`POST /workspace/graph/path`

Writing Workspace / Inquiry Graph 是 Ask PSKA 的组织层，而不是新的问答入口。
问题节点运行时仍调用 `/workspace/ask/stream`，并传入
`surface="writing"` 与 `scope={board_id,node_id}`；answer/evidence/gap 节点只保存
Ask 的结果、引用和缺口。章节 `compose` 只使用已纳入的 answer 节点生成 Markdown，
不得重新检索或绕过 Ask Router。

`/workspace/ask` 返回 `answer`、`route`、`evidence`、`citations`、`source_refs`、
`agent_steps`、`trace` 和 `timing`。UI 只展示结论、引用、证据、缺口/冲突、
agentic 检索过程和可复制 Markdown；`trace.events` 只用于调试、eval 和证明工具边界，
必须放在折叠调试区，不作为主回答正文。

一次用户问题只能有一个 retrieval owner：

- `quick`：PSKA 自己检索、组装证据和回答。若后续只需要 LLM 合成，调用 Fastreact 时必须使用 `tool_policy={"mode":"none"}`。
- `deep`：PSKA 不先预检索正文证据，只把问题、tenant/user/scope 交给 Fastreact。Fastreact 只能通过 PSKA read-only MCP 工具查库，PSKA 最后校验 citations/source refs 是否属于当前 tenant/user。
- `auto`：PSKA 根据问题形态选择 `quick` 或 `deep`，旧 direct/agentic/GraphRAG 模式不再暴露给主 UI。

Ask Router 先运行本地 PSKA planner，返回 `route.routing_owner=pska_planner`
和 `route.query_terms`，并在 stream 中发出“理解问题/选择回答路线”类
`agent_step`。这个 planner 只做关键词、路线和 GraphRAG 策略判断，不读取正文证据，
因此不算 retrieval owner，也不会造成 PSKA 和 Fastreact 双重检索。
`quick` 路径随后由 PSKA GraphRAG 检索并继续发出“检索/读取/形成回答”事件；
`deep` 路径则进入 Fastreact 事件流。

`/workspace/ask/stream` 会把 Fastreact 原始 `think/tool_call/tool_result/session_end`
事件转译为 PSKA `agent_step`：理解问题、搜索 PSKA、读取结果、继续判断、形成回答。
`agent_step` 可展示给用户；原始 Fastreact 事件仍保留在 `trace.events`，只供调试展开。

deep 路径使用的 Fastreact tool policy：

```json
{
  "mode": "allowlist",
  "allowed_tools": [
    "pska_pska_search",
    "pska_pska_index_status",
    "pska_pska_read_evidence_context",
    "pska_pska_graph_context",
    "pska_pska_digest_context"
  ]
}
```

`route.tool_profile` 为 `ask_read`。该限制必须同时发生在 Fastreact 给 LLM 暴露的
tool schema 层和工具执行层，不能只依赖 system prompt。

PSKA MCP 不只有 `pska_search`。当前按能力 profile 分层：

- `ask_read`：`pska_search`、`pska_index_status`、`pska_read_evidence_context`、`pska_graph_context`、`pska_digest_context`。只读，用于 Ask deep 和 Writing question 节点。
- `digest_worker`：`pska_job_context`、`pska_write_candidates`。仅 job-scoped digest worker 使用。
- `admin_ingest`：`pska_ingest_channel_payload`、`pska_extract_all`、`pska_review_items`。不进入普通 Ask。
- `coding_workspace`：Fastreact native `read_file/write_file/edit_file/exec`。只能用于 coding-agent profile，且必须受 tenant/user workspace path guard 和 role/purpose policy 约束。

Fastreact 的 PSKA-facing daemon 默认用全局 `tool_rules` deny native
`exec/read_file/write_file/edit_file`，不依赖 `tenant_rules.pska`。run-scoped
`tool_policy` 仍是单次请求的第一道执行边界：`quick` 合成用 `mode=none`，
`deep` 只给 `ask_read`。

## 配置

PSKA 可通过 config file 或环境变量连接 Fastreact。推荐 config file：

```json
{
  "fastreact": {
    "url": "http://127.0.0.1:18741",
    "service_token": "replace-with-local-service-token"
  },
  "agentic_service": {
    "provider": "fastreact",
    "url": "http://127.0.0.1:18741",
    "service_token": "replace-with-local-service-token"
  }
}
```

环境变量仍可覆盖 config file：

```bash
export PSKA_FASTREACT_URL="http://127.0.0.1:18741"
export PSKA_FASTREACT_SERVICE_TOKEN="replace-with-local-service-token"
```

`http://127.0.0.1:3000/service` 是 Fastreact UI。PSKA agentic search 调用的是
`http://127.0.0.1:18741` API；如果没有显式配置 `PSKA_FASTREACT_SERVICE_TOKEN`
或 config `fastreact.service_token`，PSKA `/ready` 与 `/v1/runs` 调用可能返回
401，即使 Fastreact UI 本身可以正常聊天。

调用 Fastreact 时，PSKA 使用：

- `user_key`: `pska:{user_id}`
- `metadata.caller`: `pska`
- `metadata.purpose`: `qa|extract|digest|review|memory|job`
- `metadata.pska_user_id`: PSKA represented user
- `metadata.pska_job_id`: PSKA job id
- `metadata.scope`: 允许的 source/job scope
- `tool_policy`: `none` 或 `allowlist`，用于避免重复检索和越权工具调用

Fastreact 到 PSKA MCP 的身份边界：

- 生产优先使用 HTTP MCP，并通过 AuthNode JWT 或 trusted headers 转发 tenant/user。
- stdio MCP 只适合 local/dev fallback；如果使用 stdio，Fastreact 是唯一安全边界。
- PSKA MCP schema 不把 `tenant_id/user_id` 暴露为模型可控参数；执行时始终由 RequestContext 覆盖。

## Job Orchestration

PSKA job worker 负责 durable orchestration：

- `extract_via_fastreact`
- `digest_via_fastreact`
- `review_apply`

Fastreact 执行 agentic loop，返回 `run_id`、tool events、final content 和 cited source ids。PSKA 保存必要的 `run_id` 和 job events。Fastreact 失败时，PSKA job 失败或进入 retry，不伪造成功结果。

## Readiness

PSKA `/ready` 会报告：

- database / index counts
- embedding provider config
- LLM config marker
- Fastreact health/readiness/tool loading state

Fastreact 可以离线；此时 PSKA 仍应能做基础检索、查看数据和管理 job backlog。

## 当前联调状态

2026-06-15 已用真实 LLM 验证：

- Fastreact stdio MCP 配置可加载 PSKA tools，但仅作为 local/dev fallback。
- Fastreact HTTP MCP 配置可加载 PSKA tools；生产推荐 AuthNode JWT/trusted headers，local dev 可用 service token 参数转发。
- PSKA `/ready` 可识别 Fastreact namespaced tools，例如 `pska_pska_search`。
- Fastreact `/v1/chat/completions` 可以通过 HTTP MCP 调用 PSKA MCP search 并返回 citation IDs。
- Fastreact `/v1/runs` durable path 能记录 `tool_call` 和 `tool_result` events。

HTTP MCP 现在是推荐边界；stdio MCP 仍可作为本地 fallback。
