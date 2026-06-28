# PSKA / FastReAct 层间协议

状态：draft v1  
FastReAct 事件 schema：`fastreact.agent_event.v1`

本文档定义 PSKA 与 FastReAct 的基本互联互通协议。PSKA 把
FastReAct 当作 headless agentic service layer；FastReAct 把 PSKA 当作
独立知识系统和 MCP 工具提供方。

真实端口、真实 LLM API、PSKA MCP 与 FastReAct HTTP/SSE 的操作手册见
[fastreact-pska-real-integration-manual-zh.md](fastreact-pska-real-integration-manual-zh.md)。

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
- tool policy：本次 run 可见和可执行的工具范围

不传递：

- Python object
- PSKA store/session/connection
- FastReAct Agent/Tool/Event 实例
- 未版本化的内部事件
- 原始 secrets

## PSKA 调用 FastReAct

PSKA 的产品问答入口是 `POST /workspace/ask`。该入口负责路由、租户上下文、
证据包统一、citation/source_ref 校验和 fallback。旧
`/workspace/search/query` 只作为兼容或调试入口；`/workspace/graph/path`
只作为图谱诊断、eval 和证据详情入口。

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
  "tool_policy": {
    "mode": "allowlist",
    "allowed_tools": [
      "pska_pska_search",
      "pska_pska_index_status",
      "pska_pska_read_evidence_context",
      "pska_pska_graph_context",
      "pska_pska_digest_context"
    ]
  },
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
- `tool_policy`：本次 run 的工具边界。`{"mode":"none"}` 表示 LLM 不收到任何
  tool schema，执行层也拒绝所有 tool call；`{"mode":"allowlist","allowed_tools":[...]}`
  表示只暴露和执行白名单工具。
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

## Context handoff 与事件保留原则

PSKA 不把 FastReAct 事件流等同于知识上下文。事件流按用途分层处理：

1. **Raw replay 层**：完整 `events` 只用于短期 UI 展开、debug、trace replay。它可以包含大段 tool output、路径、错误、临时推理状态，不进入 PSKA 长期知识，也不直接喂给下一轮 LLM。
2. **Run summary 层**：PSKA agentic adapter 从 FastReAct run 读取 `events`，但最终回答以最后一个 `session_end.content` 为准；`tool_call/tool_result/error/ask_user/context_compression` 被压缩成 trace summary。
3. **Prompt context 层**：下一轮传给 FastReAct 的 PSKA 上下文只应包含用户问题、必要会话摘要、source refs、citations、检索摘要、最近关键 tool state。不得传完整 raw events。
4. **Capture 层**：用户勾选 capture 或系统保存 agent answer 时，只保存 `answer`、`source_refs/citations`、压缩后的 `trace_summary` 和安全的 `tool_calls`。完整 raw events 必须丢弃。
5. **Memory/knowledge 层**：只有被证据支持、可引用、通过 ACL 和 review 策略的结论才能进入 PSKA memory/profile/graph；工具日志、模型思考、普通中间事件不能自动进入知识层。

### 事件保留/丢弃矩阵

| 事件类型 | UI replay | Prompt context | Capture | Long-term knowledge |
| --- | --- | --- | --- | --- |
| `session_start` | 可保留 | 只保留用户输入摘要 | 丢弃 raw，仅留 run/session id | 不保存 |
| `think` / planning text | 可短期显示 | 默认丢弃，可由 FastReAct 自己压缩 | 丢弃 | 不保存 |
| `tool_call` | 保留 | 保留工具名、必要参数摘要 | 保留安全字段 | 不保存 |
| `tool_result` | 保留 | 只保留摘要、citations、source ids、错误状态 | 摘要化，截断大文本 | 仅证据化结论可保存 |
| `session_end` / final answer | 保留 | 保留最终回答或摘要 | 保留 | 可进入 review/capture |
| `error` | 保留 | 保留错误类别和简述 | 保留简述 | 不保存 |
| `ask_user` / approval | 保留 | 保留待用户决策状态 | 保留决策摘要 | 不保存 |
| `context_compression` | 保留 | 保留压缩摘要 id/内容 | 保留摘要 | 仅作为 trace，不保存为事实 |
| `done` / keepalive | 丢弃 | 丢弃 | 丢弃 | 丢弃 |

### PSKA 与 FastReAct 的压缩分工

- FastReAct 压缩自己的 agent loop context：包括工具结果裁剪、上下文窗口管理、session 内部状态和 runtime replay。
- PSKA 压缩知识交接 context：包括 ACL 后的检索结果、source refs、memory/profile/graph context、用户可复用的会话摘要。
- PSKA 不应依赖 FastReAct 的内部压缩来决定什么进入知识库；进入 PSKA 的内容必须经过 PSKA 自己的 citation、ACL、review 和 retention 策略。
- 当 FastReAct 已发出 context compression 事件时，PSKA 可以把它作为 trace summary 的一部分，但不能把它当作已验证知识。

当前实现约束：

- Workspace UI 可展开 `Raw FastReAct events`，但这是短期调试视图。
- `capture_agent_conversation` 路径使用 `compact_trace_for_context`，不会保存完整 `trace.events`。
- `session_end.content` 是最终回答的优先来源；非流式 `content/answer` 只作为 fallback。

## PSKA MCP Tool Contract

PSKA 通过 MCP 提供工具，FastReAct 通过部署配置加载。

当前 MCP 工具按能力 profile 分层：

- `ask_read`：`pska_search`、`pska_index_status`、`pska_read_evidence_context`、`pska_graph_context`、`pska_digest_context`。
- `digest_worker`：`pska_job_context`、`pska_write_candidates`。
- `admin_ingest`：`pska_ingest_channel_payload`、`pska_extract_all`、`pska_review_items`。
- `coding_workspace`：FastReAct native `read_file/write_file/edit_file/exec`，不属于 PSKA MCP。

工具参数原则：

- 查询类工具通常接收 `query`，也可以接收 citations/source refs/entity ids 做二次读取。
- HTTP MCP 下 tenant/user 不由模型参数决定；PSKA 从 RequestContext 覆盖执行参数。
- 检索工具应接收 `top_k`。
- 写入/提取/review 类工具必须由 PSKA 自己校验权限。

工具结果原则：

- 返回内容应可 JSON 序列化。
- 检索/agentic-search 结果应包含 citations 或 source ids。
- 结果可以包含 trace/gaps/confidence，但不能包含 secrets。
- 权限拒绝必须作为明确 error 返回，不能静默返回空成功。

Ask PSKA deep 路径只允许 read-only 查询工具：

- `pska_pska_search`
- `pska_pska_index_status`
- `pska_pska_read_evidence_context`
- `pska_pska_graph_context`
- `pska_pska_digest_context`

不得在 deep 问答中调用写入、review apply、job mutation
或 filesystem/host 工具。FastReAct run trace 必须记录 `tool_policy`、实际 tool calls
和 denied tool calls，便于证明一次问题没有重复检索 owner。

## Identity And ACL

FastReAct 只透传身份上下文：

- HTTP request `user_key`
- HTTP metadata `pska_user_id`
- HTTP MCP AuthNode JWT/trusted headers
- local/dev stdio MCP params

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
