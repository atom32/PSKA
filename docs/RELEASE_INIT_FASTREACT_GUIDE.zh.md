# PSKA 发布、初始化与 FastReAct 联动指南

这份文档面向本地发布和新环境初始化：从空 checkout 到可运行的 PSKA
Workspace，再到 FastReAct 真实联调。PSKA 负责知识库、权限、引用、HTTP
API 和 MCP；FastReAct 负责 agentic loop、LLM 调用、工具编排和事件流。

## 1. 推荐目录与端口

本文用占位变量表示本机 checkout，不记录任何真实用户名或机器路径：

```bash
export PSKA_REPO="/path/to/pska"
export FASTREACT_NANO_REPO="/path/to/FastReAct/fastreact-nano"
```

源码仓库：

```text
$PSKA_REPO
```

PSKA 本地运行数据：

```text
~/PSKA_workspaces/default
```

默认端口：

```text
PSKA HTTP service: http://127.0.0.1:8765
PSKA frontend:     http://127.0.0.1:5173
FastReAct API:     http://127.0.0.1:18741
```

不要把 `.pska/`、`~/PSKA_workspaces/default`、`~/.fastreact/credentials.json`
或真实密钥提交到仓库。

## 2. 首次初始化

安装 Python 3.12 并创建 PSKA 虚拟环境：

```bash
brew install python@3.12
cd "$PSKA_REPO"
./scripts/bootstrap_pska_env
```

创建本地配置。`./start.sh` 在缺少配置时会自动从
`core/config.pska.example.json` 复制到 `.pska/config.json`，也可以手动创建：

```bash
mkdir -p .pska
cp core/config.pska.example.json .pska/config.json
```

推荐确认这些字段：

```json
{
  "database": {"url": "postgresql:///pska"},
  "workspace": {"root": "~/PSKA_workspaces/default"},
  "service": {"host": "127.0.0.1", "port": 8765, "service_token": null},
  "fastreact": {"url": "http://127.0.0.1:18741", "service_token": null}
}
```

初始化数据库：

```bash
./scripts/pska --config .pska/config.json db-check
./scripts/pska --config .pska/config.json db-create --name pska
./scripts/pska --config .pska/config.json db-init
```

如果是首次安装前端依赖：

```bash
cd frontend
npm install
cd ..
```

## 3. 日常启动

最常用方式是一条命令启动 PSKA 后端 supervisor 和前端：

```bash
./start.sh
```

它会自动执行：

- 准备数据库和 migrations。
- 创建默认 Knowledge Source：`~/PSKA_workspaces/default/notes`。
- 尝试执行一次 `files-sync`。
- 启动 `local-daemon --restart`，包含 `serve`、`job-worker`、`digest-scheduler`。
- 启动 Vite frontend。

打开：

```text
http://127.0.0.1:5173/
```

停止：

```text
在 ./start.sh 所在终端按 Ctrl-C
```

只启动后端或只启动前端：

```json
"startup": {
  "backend": true,
  "frontend": { "enabled": false }
}
```

或：

```json
"startup": {
  "backend": false,
  "frontend": { "enabled": true }
}
```

跳过启动前的 bootstrap：

```json
"startup": {
  "bootstrap": false
}
```

改完 `.pska/config.json` 后再运行 `./start.sh`。

## 4. 发布前检查

本地发布或切换环境前，先确认服务、数据库和 MCP contract 对齐：

```bash
./scripts/pska --config .pska/config.json service-check
./scripts/pska --config .pska/config.json local-daemon status
./scripts/pska --config .pska/config.json daily-status
```

如果服务启用了 token：

```bash
./scripts/pska --config .pska/config.json service-check \
  --service-token "<service.service_token>"
```

检查前端代理是否能读到真实后端数据：

```bash
curl -sS "http://127.0.0.1:8765/workspace/today/data?owner_user_id=user_primary&limit=3" \
  | ./.pska/venvs/pska-py312/bin/python -m json.tool
```

冷启动 E2E：

```bash
./scripts/pska-cold-start-e2e --workspace-root ~/PSKA_workspaces/default --reset
```

`--reset` 只会删除带 `.pska_workspace.json` 哨兵的 workspace，避免误删任意目录。

## 5. FastReAct 联动模式

推荐把 PSKA 和 FastReAct 当作两个独立服务：

```text
PSKA service  <-- HTTP MCP -->  FastReAct service
```

这种方式下 PSKA 需要先用 `./start.sh` 或 `local-daemon` 启动，FastReAct
通过 `http://127.0.0.1:8765/mcp` 调用 PSKA 工具。好处是 readiness、token、
日志、canonical DB 对齐都由 PSKA service 统一负责。

生成 FastReAct HTTP MCP 配置：

```bash
./scripts/fastreact-pska-service-config \
  --mcp-transport http \
  --output .pska/fastreact-pska-http.json
```

如果 PSKA HTTP service 启用了 token，需要把同一个 token 写到 FastReAct
credentials。示例文件位置：

```text
~/.fastreact/credentials.json
```

示例结构：

```json
{
  "mcp_api_keys": {
    "pska": "<pska-local-service-token>"
  }
}
```

启动 FastReAct：

```bash
cd "$FASTREACT_NANO_REPO"
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src \
  python3 -m fastreact.adapters.http \
  --config "$PSKA_REPO/.pska/fastreact-pska-http.json"
```

FastReAct 默认 API 地址：

```text
http://127.0.0.1:18741
```

PSKA 调用 FastReAct 时需要知道 FastReAct API 和 service token：

```json
"fastreact": {
  "url": "http://127.0.0.1:18741",
  "service_token": "<fastreact-service-token>",
  "timeout_seconds": 30
}
```

## 6. FastReAct 兼容 stdio MCP 模式

如果只是验证 FastReAct 能否拉起 PSKA MCP 子进程，可以使用默认 stdio 配置：

```bash
./scripts/fastreact-pska-service-config
cd "$FASTREACT_NANO_REPO"
python3 -m fastreact.adapters.http --config ~/.fastreact/config.json
```

stdio 模式下 FastReAct 会启动：

```text
$PSKA_REPO/scripts/pska mcp-server
```

这种模式不要求 PSKA HTTP daemon 已经启动，但必须确认生成配置时使用的是
canonical DB：

```bash
./scripts/fastreact-pska-service-config --database-url postgresql:///pska
```

日常联调优先使用 HTTP MCP；stdio 更适合最小化验证 MCP 子进程加载。

## 7. 联调验证

PSKA 侧：

```bash
./scripts/pska --config .pska/config.json service-check
```

FastReAct readiness：

```bash
TOKEN="<fastreact-service-token>"
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:18741/ready | python3 -m json.tool
```

关键字段：

```text
service_contract = fastreact.agent_event.v1
auth.required = true
mcp.ready = true
mcp.tools 包含 pska_pska_search
```

真实 HTTP/SSE smoke：

```bash
cd "$PSKA_REPO/core"
python3 scripts/fastreact_http_sse_e2e.py \
  --python ../.pska/venvs/pska-py312/bin/python
```

成功时输出包含：

```json
{"ok": true}
```

## 8. 常见问题

`service-check` 失败：

```bash
./scripts/pska --config .pska/config.json local-daemon status
tail -f ~/PSKA_workspaces/default/logs/pska-service.log
```

端口被占用：

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
lsof -nP -iTCP:18741 -sTCP:LISTEN
```

FastReAct 返回 401：

- PSKA 调 FastReAct：检查 `.pska/config.json` 的 `fastreact.service_token`。
- FastReAct 调 PSKA HTTP MCP：检查 `~/.fastreact/credentials.json` 里的
  `mcp_api_keys.pska` 是否等于 PSKA service token。

FastReAct `/ready` 中 `mcp.ready=false`：

```bash
./scripts/fastreact-pska-service-config \
  --mcp-transport http \
  --output .pska/fastreact-pska-http.json
./scripts/pska --config .pska/config.json service-check
```

确认 FastReAct 配置里的 PSKA MCP endpoint 是：

```text
http://127.0.0.1:8765/mcp
```

Digest backlog 堆积：

```bash
./scripts/pska --config .pska/config.json jobs stats
./scripts/pska --config .pska/config.json fastreact-digest-worker-command
```

`digest_via_fastreact` 由 FastReAct worker 消费；PSKA local worker 负责本地
job 和调度，不直接执行 LLM digest。
