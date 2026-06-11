# PSKA / FastReAct 真实联调 Manual

本文档用于在本机以真实端口、真实 LLM API、真实 PSKA MCP server 的方式联调：

- PSKA 作为知识系统与 MCP 工具提供方。
- FastReAct 作为 headless agentic service layer。
- 两边只通过 HTTP/SSE、service token、`fastreact.agent_event.v1` 和 PSKA MCP 交互。

## 前置条件

确认 key 文件存在：

```bash
test -f "$HOME/api_key.txt" && wc -c "$HOME/api_key.txt"
```

推荐把 `~/api_key.txt` 写成 JSON：

```json
{
  "api_key": "sk-...",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "service_token": "replace-with-local-service-token"
}
```

PSKA 仍兼容旧三行格式：

```text
line 1: LLM API key
line 2: model，例如 deepseek-v4-flash
line 3: OpenAI-compatible base URL，例如 https://api.deepseek.com
line 4: 可选 FastReAct service token
```

注意：实际文件名是 `api_key.txt`，不是 `api-key.txt`。

## 环境变量

在一个 shell 中准备共享环境：

```bash
eval "$(
python3 - <<'PY'
import json
import shlex
from pathlib import Path

path = Path.home() / "api_key.txt"
text = path.read_text(encoding="utf-8").strip()
if text.startswith("{"):
    data = json.loads(text)
    key = data.get("api_key") or data.get("key") or ""
    model = data.get("model") or ""
    base = data.get("base_url") or data.get("api_base") or ""
    service_token = data.get("service_token") or data.get("fastreact_service_token") or "replace-with-local-service-token"
else:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    key = lines[0] if len(lines) > 0 else ""
    model = lines[1] if len(lines) > 1 else ""
    base = lines[2] if len(lines) > 2 else ""
    service_token = lines[3] if len(lines) > 3 else "replace-with-local-service-token"

for name, value in {
    "KEY": key,
    "MODEL": model,
    "BASE": base,
    "FASTREACT_SERVICE_TOKEN": service_token,
}.items():
    print(f"export {name}={shlex.quote(value)}")
PY
)"

export FASTRACT_API_KEY="$KEY"
export FASTRACT_MODEL="$MODEL"
export FASTRACT_API_BASE="$BASE"
export OPENAI_API_KEY="$KEY"

export PSKA_LLM_API_KEY_FILE="$HOME/api_key.txt"
export SSL_CERT_FILE=$("/Users/xudawei/Documents/personal archive/.pska/venvs/pska-py312/bin/python" -c 'import certifi; print(certifi.where())')
```

`SSL_CERT_FILE` 很重要。本机 Python 默认 CA 路径可能导致：

```text
SSL: CERTIFICATE_VERIFY_FAILED
self-signed certificate in certificate chain
```

设置到 `certifi` 后，PSKA 的真实 LLM 请求已验证可通过。

## 启动 FastReAct HTTP Service

```bash
cd /Users/xudawei/FastReAct/fastreact-nano

export FASTRACT_MCP_SERVERS='[{"name":"pska","command":"/Users/xudawei/Documents/personal archive/scripts/pska","args":["mcp-server"],"isolation":"shared","description":"PSKA tools"}]'

python3 -m fastreact.adapters.http
```

默认监听：

```text
http://127.0.0.1:8000
```

## Readiness 检查

另开一个 shell，带 service token 调用：

```bash
curl -sS \
  -H "Authorization: Bearer $FASTREACT_SERVICE_TOKEN" \
  http://127.0.0.1:8000/ready | python3 -m json.tool
```

关键字段应满足：

```text
service_contract = fastreact.agent_event.v1
auth.required = true
mcp.ready = true
mcp.tools 包含 pska_pska_search
```

如果没有设置 `FASTREACT_SERVICE_TOKEN`，本地开发允许无鉴权；部署联调建议始终设置。

## 真实 E2E Smoke

PSKA repo 提供了一个确定性 E2E smoke，验证真实 HTTP/SSE service、service auth、真实 PSKA MCP JSON-RPC 子进程和 SSE event contract：

```bash
cd "/Users/xudawei/Documents/personal archive/core"

python3 scripts/fastreact_http_sse_e2e.py \
  --python ../.pska/venvs/pska-py312/bin/python
```

成功时输出包含：

```json
{
  "ok": true,
  "event_types": ["session_start", "tool_call", "tool_result", "session_end"],
  "schema_header": "fastreact.agent_event.v1"
}
```

这个 smoke 不依赖真实外部 LLM；它用于验证服务边界和 PSKA MCP 真实链路。

## 真实 LLM API Smoke

PSKA LLM smoke：

```bash
cd "/Users/xudawei/Documents/personal archive/core"

PSKA_LLM_API_KEY_FILE="$HOME/api_key.txt" \
SSL_CERT_FILE=$(../.pska/venvs/pska-py312/bin/python -c 'import certifi; print(certifi.where())') \
../.pska/venvs/pska-py312/bin/python - <<'PY'
from pska_core.llm import OpenAILLMClient

client = OpenAILLMClient.from_env()
print("model", client.model)
print("base_url", client.base_url)
result = client.complete_json(
    system="Return JSON only.",
    prompt='Return {"ok": true, "component": "pska"}.',
    temperature=0,
)
print(result)
PY
```

FastReAct LLM smoke：

```bash
cd /Users/xudawei/FastReAct/fastreact-nano

export SSL_CERT_FILE=$(python3 -c 'import certifi; print(certifi.where())')

python3 - <<'PY'
import asyncio
import os
from fastreact.providers.litellm import LiteLLMProvider

async def main():
    llm = LiteLLMProvider(
        model=os.environ["MODEL"],
        api_key=os.environ["KEY"],
        api_base=os.environ["BASE"],
        temperature=0,
        max_tokens=100,
    )
    response = await llm.chat([
        {"role": "system", "content": "Reply with exactly JSON and nothing else."},
        {"role": "user", "content": 'Return {"ok": true, "component": "fastreact"}'},
    ])
    print(response.content)

asyncio.run(main())
PY
```

## PSKA 报告走 FastReAct API

FastReAct service 启动后，运行 PSKA 报告侧 API 模式：

```bash
cd "/Users/xudawei/Documents/personal archive/core"

FASTREACT_API_URL="http://127.0.0.1:8000" \
FASTREACT_SERVICE_TOKEN="$FASTREACT_SERVICE_TOKEN" \
PSKA_LLM_API_KEY_FILE="$HOME/api_key.txt" \
SSL_CERT_FILE=$(../.pska/venvs/pska-py312/bin/python -c 'import certifi; print(certifi.where())') \
../.pska/venvs/pska-py312/bin/python scripts/twitter_full_report.py \
  --fastreact-mode api
```

报告侧会：

- 调用 FastReAct `/v1/chat/completions`。
- 携带 `Authorization: Bearer <token>` 和 `X-FastReAct-Service-Token`。
- 解析 SSE `tool_call`、`tool_result`、`session_end`、`done`。
- 将 FastReAct trace 纳入报告。

## 常见问题

### 401 Unauthorized

确认请求 header 中 token 与 FastReAct service 启动时的 token 一致：

```bash
echo "$FASTREACT_SERVICE_TOKEN"
```

### `/ready` 中 `mcp.ready=false`

确认 `FASTRACT_MCP_SERVERS` JSON 中的 `command` 指向 PSKA 脚本：

```text
/Users/xudawei/Documents/personal archive/scripts/pska
```

并确认 PSKA 数据库环境变量正确。如果使用默认本地库，通常不需要额外设置；如果指定数据库：

```bash
export PSKA_DATABASE_URL="postgresql:///pska"
```

### DeepSeek / OpenAI-compatible API 证书失败

设置：

```bash
export SSL_CERT_FILE=$(python3 -c 'import certifi; print(certifi.where())')
```

PSKA venv 下建议用：

```bash
export SSL_CERT_FILE=$("/Users/xudawei/Documents/personal archive/.pska/venvs/pska-py312/bin/python" -c 'import certifi; print(certifi.where())')
```

### FastReAct 直接打到 OpenAI 而不是 DeepSeek

显式设置：

重新执行本文档“环境变量”一节的 `eval "$(python3 ...)"` 代码块，确保
`FASTRACT_MODEL`、`FASTRACT_API_BASE`、`FASTRACT_API_KEY` 都来自同一份
`~/api_key.txt`。

对底层 LiteLLM smoke，最好显式传 `api_base`，避免走默认 OpenAI endpoint。

### Headroom memory 失败

这不影响 PSKA/FastReAct 联调。Headroom 是 Codex 记忆层。当前已知问题是
`Qdrant/all-MiniLM-L6-v2-onnx/model.onnx` 没完整缓存，只影响 Codex memory。

## 验收标准

一次完整联调成功应满足：

- `GET /ready` 返回 `auth.required=true`、`mcp.ready=true`。
- `mcp.tools` 包含 `pska_pska_search`。
- `POST /v1/chat/completions` 返回 `X-FastReAct-Event-Schema=fastreact.agent_event.v1`。
- SSE 包含 `session_start`、`tool_call`、`tool_result`、`session_end`、`done`。
- PSKA 报告侧使用 FastReAct API 模式时不再 fallback 到 local import。
