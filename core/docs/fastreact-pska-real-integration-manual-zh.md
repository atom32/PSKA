# PSKA / FastReAct 真实联调 Manual

目标：FastReAct 作为 headless agentic service layer，PSKA 作为独立 MCP 工具服务。两边只通过 HTTP/SSE、service token、`fastreact.agent_event.v1` 和 PSKA MCP 交互。

## 一次性配置

推荐把 `~/api_key.txt` 写成 JSON：

```json
{
  "api_key": "sk-...",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "service_token": "replace-with-local-service-token"
}
```

PSKA 和 FastReAct 仍兼容旧行格式：

```text
line 1: LLM API key
line 2: model，例如 deepseek-v4-flash
line 3: OpenAI-compatible base URL，例如 https://api.deepseek.com
line 4: 可选 FastReAct service token
```

生成 FastReAct 本地配置：

```bash
cd "/Users/xudawei/Documents/personal archive"
./scripts/fastreact-pska-service-config
```

默认写入：

```text
~/.fastreact/config.json
```

这个配置会包含：

- `llm.api_key_file = ~/api_key.txt`
- `service.host = 0.0.0.0`
- `service.port = 8000`
- `service.service_token` 从 `~/api_key.txt` 的 `service_token` 读取
- `mcp.servers[0]` 指向 PSKA 的 `scripts/pska mcp-server`
- PSKA MCP 子进程 env，包括 `PSKA_LLM_API_KEY_FILE`、`PSKA_DATABASE_URL` 和可探测到的 `SSL_CERT_FILE`

如需查看而不写入：

```bash
./scripts/fastreact-pska-service-config --print
```

## 启动 FastReAct

日常启动只需要：

```bash
cd /Users/xudawei/FastReAct/fastreact-nano
python3 -m fastreact.adapters.http
```

如果不想使用默认 `~/.fastreact/config.json`，也可以显式指定：

```bash
python3 -m fastreact.adapters.http --config ~/.fastreact/config.json
```

默认监听：

```text
http://127.0.0.1:8000
```

PSKA 不需要另起 HTTP daemon。FastReAct 会按 config 启动 PSKA MCP stdio 子进程。

## Readiness 检查

另开一个 shell：

```bash
TOKEN=$(python3 - <<'PY'
import json
from pathlib import Path
data = json.loads((Path.home() / "api_key.txt").read_text())
print(data.get("service_token") or data.get("fastreact_service_token") or "")
PY
)

curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/ready | python3 -m json.tool
```

关键字段应满足：

```text
service_contract = fastreact.agent_event.v1
auth.required = true
mcp.ready = true
mcp.tools 包含 pska_pska_search
```

## 手动请求

```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/chat/completions \
  -d '{
    "messages": [
      {"role": "system", "content": "Use PSKA MCP tools and cite evidence."},
      {"role": "user", "content": "搜索 PSKA 中和 FastReAct 相关的信息"}
    ],
    "stream": false,
    "user_key": "pska:user_primary",
    "metadata": {"caller": "pska", "run_id": "manual-fastreact-pska"}
  }' | python3 -m json.tool
```

## 真实 E2E Smoke

PSKA repo 提供确定性 E2E smoke，验证真实 HTTP/SSE service、service auth、真实 PSKA MCP JSON-RPC 子进程和 SSE event contract：

```bash
cd "/Users/xudawei/Documents/personal archive/core"
python3 scripts/fastreact_http_sse_e2e.py \
  --python ../.pska/venvs/pska-py312/bin/python
```

成功时输出包含：

```json
{
  "ok": true
}
```

smoke 不依赖真实外部 LLM；它用于验证服务边界和 PSKA MCP 真实链路。

## PSKA 报告走 FastReAct API

FastReAct service 启动后：

```bash
cd "/Users/xudawei/Documents/personal archive/core"
TOKEN=$(python3 - <<'PY'
import json
from pathlib import Path
data = json.loads((Path.home() / "api_key.txt").read_text())
print(data.get("service_token") or data.get("fastreact_service_token") or "")
PY
)

FASTREACT_API_URL="http://127.0.0.1:8000" \
FASTREACT_SERVICE_TOKEN="$TOKEN" \
PSKA_LLM_API_KEY_FILE="$HOME/api_key.txt" \
../.pska/venvs/pska-py312/bin/python scripts/twitter_full_report.py \
  --fastreact-mode api
```

## 常见问题

### 401 Unauthorized

确认请求 header 中 token 与 `~/api_key.txt` 的 `service_token` 一致。

### `/ready` 中 `mcp.ready=false`

重新生成配置并重启 FastReAct：

```bash
cd "/Users/xudawei/Documents/personal archive"
./scripts/fastreact-pska-service-config
```

确认 `~/.fastreact/config.json` 里的 MCP command 指向：

```text
/Users/xudawei/Documents/personal archive/scripts/pska
```

### DeepSeek / OpenAI-compatible API 证书失败

`scripts/fastreact-pska-service-config` 会尝试把 PSKA venv 的 `certifi` 路径写入 MCP 子进程 env。若仍失败，确认：

```bash
"/Users/xudawei/Documents/personal archive/.pska/venvs/pska-py312/bin/python" -c 'import certifi; print(certifi.where())'
```
