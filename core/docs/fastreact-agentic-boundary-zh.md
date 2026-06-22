# PSKA / Fastreact Agentic Boundary

日期：2026-06-14

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

## 配置

PSKA 可通过 config file 或环境变量连接 Fastreact。推荐 config file：

```json
{
  "llm": {"api_key_file": "~/api_key.txt"},
  "fastreact": {
    "url": "http://127.0.0.1:8000",
    "service_token": "replace-with-local-service-token"
  }
}
```

环境变量仍可覆盖 config file：

```bash
export PSKA_FASTREACT_URL="http://127.0.0.1:8000"
export PSKA_FASTREACT_SERVICE_TOKEN="replace-with-local-service-token"
```

`http://127.0.0.1:3000/service` 是 Fastreact UI。PSKA agentic search 调用的是
`http://127.0.0.1:8000` API；如果没有显式配置 `PSKA_FASTREACT_SERVICE_TOKEN`
或 config `fastreact.service_token`，PSKA `/ready` 与 `/v1/runs` 调用可能返回
401，即使 Fastreact UI 本身可以正常聊天。

调用 Fastreact 时，PSKA 使用：

- `user_key`: `pska:{user_id}`
- `metadata.caller`: `pska`
- `metadata.purpose`: `qa|extract|digest|review|memory|job`
- `metadata.pska_user_id`: PSKA represented user
- `metadata.pska_job_id`: PSKA job id
- `metadata.scope`: 允许的 source/job scope

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

- Fastreact stdio MCP 配置可加载 PSKA tools。
- Fastreact HTTP MCP 配置可加载 PSKA tools，需要 `auth_token_ref=mcp_api_keys.pska` 和 `~/.fastreact/credentials.json` 中的 PSKA service token。
- PSKA `/ready` 可识别 Fastreact namespaced tools，例如 `pska_pska_search`。
- Fastreact `/v1/chat/completions` 可以通过 HTTP MCP 调用 PSKA MCP search 并返回 citation IDs。
- Fastreact `/v1/runs` durable path 能记录 `tool_call` 和 `tool_result` events。

HTTP MCP 现在是推荐边界；stdio MCP 仍可作为本地 fallback。
