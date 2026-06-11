# PSKA / FastReAct 层间协议

状态：draft v1  
FastReAct 事件 schema：`fastreact.agent_event.v1`

本文档定义 PSKA 与 FastReAct 的基本互联互通协议。PSKA 把
FastReAct 当作 headless agentic service layer；FastReAct 把 PSKA 当作
独立知识系统和 MCP 工具提供方。

## 边界原则

PSKA 负责：

- 知识存储
- ACL、visibility、team/user 权限
- review workflow
- ingest / extraction / report jobs
- source id、citation、knowledge trace
- PSKA MCP tools

FastReAct 负责：

- agent planning loop
- LLM 调用
- tool orchestration
- session / run lifecycle
- event streaming
- runtime trace 和 tool audit

禁止默认耦合：

- PSKA 生产路径不 import FastReAct internals。
- FastReAct 不 import PSKA internals。
- FastReAct 不直接访问 PSKA DB。
- FastReAct 不替 PSKA 做知识 ACL 判断。
- 真实 API key、PAT、数据库连接对象不能跨协议传递。

## 我们传递的是什么

跨层传递的是协议数据，不是内部对象：

- run request：system/user messages、stream 模式、session id、metadata
- identity context：`user_key`、`pska_user_id`、caller、purpose
- agent events：生命周期、思考、工具调用、工具结果、最终回答、错误、审批请求
- tool calls：MCP tool name 和 JSON arguments
- tool results：工具返回的 JSON/text、citation/source ids、trace、error
- response summary：final answer、events、tool calls、duration、run id、session id
- health/readiness：模型配置状态、MCP readiness、loaded tools、依赖状态

不传递：

- Python object
- PSKA store/session/connection
- FastReAct Agent/Tool/Event 实例
- 未版本化的内部事件
- 原始 secrets

## PSKA 调用 FastReAct

Endpoint：

```http
POST /v1/chat/completions
```

请求示例：

```json
{
  "messages": [
    {
      "role": "system",
      "content": "Use PSKA MCP tools and cite evidence."
    },
    {
      "role": "user",
      "content": "Question"
    }
  ],
  "stream": true,
  "session_id": "optional-session-id",
  "user_key": "pska:user_primary",
  "skills": ["optional-skill-name"],
  "metadata": {
    "caller": "pska",
    "run_id": "optional-report-run-id",
    "purpose": "qa|report|review|memory|job",
    "pska_user_id": "user_primary"
  }
}
```

字段含义：

- `messages`：对话输入。必须至少包含一个非空 user message。
- `stream`：`true` 返回 SSE；`false` 返回汇总 JSON。
- `session_id`：可选。用于续接 FastReAct session。
- `user_key`：FastReAct 侧用户/租户隔离 key。
- `metadata.caller`：调用方，PSKA 固定用 `pska`。
- `metadata.run_id`：PSKA report/job 可以传自己的 run id，便于串联 trace。
- `metadata.purpose`：调用目的，例如 `qa`、`report`、`review`、`memory`、`job`。
- `metadata.pska_user_id`：PSKA 知识 ACL 使用的用户 id。

## PSKA 消费 FastReAct 事件

SSE frame：

```text
event: tool_call
data: {"schema":"fastreact.agent_event.v1","type":"tool_call"}
```

事件 payload：

```json
{
  "schema": "fastreact.agent_event.v1",
  "type": "session_start|think|tool_call|tool_result|session_end|error|ask_user",
  "event_id": "run-id:0",
  "parent_event_id": null,
  "run_id": "run-id",
  "session_id": "session-id",
  "timestamp": "2026-06-11T00:00:00+00:00",
  "content": "human-readable event content",
  "tool_name": "pska_pska_search",
  "tool_args": {
    "query": "Question",
    "user_id": "user_primary",
    "top_k": 5
  },
  "tool_call_id": "call-id",
  "duration_ms": null,
  "cited_source_ids": ["source-id-1"],
  "metadata": {}
}
```

流结束：

```text
event: done
data: [DONE]
```

PSKA consumer 规则：

- 必须按 `schema` 选择解析逻辑。
- 必须忽略未知字段。
- 应容忍新增 `type`。
- 不能解析 FastReAct 内部 Python event。
- `session_end` 的 `content` 是最终回答。
- `tool_call` / `tool_result` 可用于报告展示和 trace。
- `error` 应进入 PSKA report/job error path，不应伪造成功答案。

## 非流式响应

当 `stream=false`：

```json
{
  "type": "chat.completion",
  "run_id": "run-id",
  "session_id": "session-id",
  "content": "final answer",
  "events": [],
  "tool_calls": [
    {
      "event_id": "run-id:1",
      "tool_call_id": "call-id",
      "tool_name": "pska_pska_search",
      "tool_args": {}
    }
  ],
  "duration_ms": 1234.56,
  "metadata": {
    "schema": "fastreact.agent_event.v1",
    "event_count": 4
  }
}
```

PSKA report/job 可以优先使用 SSE，以便展示过程；批处理或简单 QA 可以使用
`stream=false`。

## PSKA MCP Tool Contract

PSKA 通过 MCP 提供工具，FastReAct 通过部署配置加载。

当前工具：

- `pska_search`
- `pska_agentic_search`
- `pska_index_status`
- `pska_ingest_channel_payload`
- `pska_extract_all`
- `pska_review_items`

工具参数原则：

- 查询类工具必须接收 `query`。
- ACL 相关工具必须接收 `user_id` 或 `owner_user_id`。
- 检索工具应接收 `top_k`。
- 写入/提取/review 类工具必须由 PSKA 自己校验权限。

工具结果原则：

- 返回内容应可 JSON 序列化。
- 检索/agentic-search 结果应包含 citations 或 source ids。
- 结果可以包含 trace/gaps/confidence，但不能包含 secrets。
- 权限拒绝必须作为明确 error 返回，不能静默返回空成功。

## Identity And ACL

FastReAct 只透传身份上下文：

- HTTP request `user_key`
- HTTP metadata `pska_user_id`
- MCP tool argument `user_id`
- MCP tool argument `owner_user_id`

PSKA 必须自己决定：

- 用户能否读取 source/chunk/entity
- 用户能否运行 ingest/extract
- review item 能否 approve/reject/apply
- 哪些 citation/source ids 可以返回

## FastReAct Health / Readiness / Auth

`GET /health` 是公开轻量健康检查，只保证 HTTP service 存活，并返回
`service_contract=fastreact.agent_event.v1`。

`GET /ready` 是部署 readiness contract。部署设置 `FASTREACT_SERVICE_TOKEN`
后，PSKA 必须携带以下任一 header：

```http
Authorization: Bearer <token>
X-FastReAct-Service-Token: <token>
```

`/ready` 必须主动确认：

- `agent_ready=true`
- `service_contract=fastreact.agent_event.v1`
- `auth.required`
- `model.name`、`model.api_base_configured`、`model.api_key_configured`
- `mcp.ready`
- `mcp.servers[].name/alive`
- `mcp.tools` 包含 PSKA 工具，例如 `pska_pska_search`

`POST /v1/chat/completions` 在设置 `FASTREACT_SERVICE_TOKEN` 后也必须要求
同一 service token。管理面 admin key 不等同于 PSKA service token。

真实 E2E 验收命令：

```bash
cd core
python3 scripts/fastreact_http_sse_e2e.py --python ../.pska/venvs/pska-py312/bin/python
```

该脚本启动真实 FastReAct localhost HTTP/SSE service、真实 PSKA MCP JSON-RPC
子进程，验证 `/ready`、service auth、SSE `tool_call/tool_result/session_end/done`
和 PSKA MCP evidence 返回。

## Versioning

- 当前 event schema 是 `fastreact.agent_event.v1`。
- 新增字段不需要升级 major version。
- 删除或改名字段必须升级 schema version。
- PSKA 测试应固定期望 schema，并允许未知字段。

## 最小互通测试

PSKA 与 FastReAct 的最小互通测试应覆盖：

1. 启动 PSKA MCP server。
2. FastReAct 通过配置加载 PSKA MCP。
3. PSKA 调用 FastReAct `POST /v1/chat/completions`。
4. 事件序列包含 `session_start`、`tool_call`、`tool_result`、`session_end`。
5. 最终回答包含 `run_id`、`session_id`。
6. PSKA 工具返回 citation/source ids 时，报告里能展示证据链。
