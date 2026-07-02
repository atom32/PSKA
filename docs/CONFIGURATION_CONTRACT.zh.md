# PSKA / FastReAct / AuthNode 配置约定

更新时间：2026-07-01

这份文档记录三套本地服务的配置来源、启动入口和兼容字段。新环境排查时先看本页，避免改了一个配置文件，但服务实际读取了另一个文件。

## 推荐启动入口

本地联调时优先使用每个仓库根目录的 `start.sh`：

```bash
cd "$AUTHNODE_REPO" && ./start.sh
cd "$FASTREACT_REPO" && ./start.sh
cd "$PSKA_REPO" && ./start.sh
```

PSKA 的开发验证必须使用 PSKA 仓库的 `./start.sh`，因为它会按 `.pska/config.json` 同时启动 backend supervisor 和 gateway/frontend。

## 服务地址

| Service | 默认地址 | 说明 |
| --- | --- | --- |
| AuthNode | `http://127.0.0.1:8788` | 登录、JWT、代理和本地 IAM |
| PSKA backend | `http://127.0.0.1:8765` | PSKA HTTP API / MCP |
| PSKA gateway/frontend | `http://127.0.0.1:5173` | 生产式前端入口；`/login` 由 gateway 跳 AuthNode |
| FastReAct daemon | `http://127.0.0.1:18741` | Agentic Ask / digest 执行 |

如果 PSKA 登录只停在 `http://127.0.0.1:5173/login`，通常说明当前跑的是 Vite/dev frontend，或者 gateway 被配置为本地 token-broker 模式。只有 PSKA gateway 且 `PSKA_GATEWAY_AUTHNODE_BROWSER_LOGIN=true` 时，`/login` 才会跳到 AuthNode：

```text
http://127.0.0.1:8788/login?target=pska&return_to=...
```

Docker 部署中不要把浏览器跳转地址写成容器内服务名。保持
`PSKA_GATEWAY_AUTHNODE_URL=http://authnode:8788` 给服务端调用，同时设置
`PSKA_GATEWAY_AUTHNODE_BROWSER_URL=http://<public-host>:8788` 给用户浏览器。

## 配置文件位置

新环境统一使用仓库内隐藏目录里的 `config.json`：

| Service | 新环境配置文件 | 兼容旧路径 |
| --- | --- | --- |
| PSKA | `.pska/config.json` | `~/.pska/config.json`、`config.pska.json` 仅在不通过 `./start.sh` 且不显式传 `--config` 时参与查找 |
| FastReAct | `.fastreact/config.json` | `~/.fastreact/config.json`、`fastreact-nano/.fastreact/config.json`、`fastreact-nano/config.json` |
| AuthNode | `.authnode/config.json` | `authnode.local.json` |

### PSKA

推荐运行配置：

```text
.pska/config.json
```

生成来源：

```text
core/config.pska.example.json
```

`./start.sh` 固定使用仓库内 `.pska/config.json`。PSKA 代码在不显式传 `--config` 时也会尝试 `~/.pska/config.json`、当前目录 `.pska/config.json` 和 `config.pska.json`，但本仓库启动脚本会显式传入仓库内配置。

当前 runtime 配置是 JSON。`core/config.example.toml` 和本机 `.pska/config.toml` 属于旧配置口径，不参与当前 PSKA service/gateway 启动。

### FastReAct

推荐运行配置：

```text
.fastreact/config.json
```

当前兼容运行配置：

```text
~/.fastreact/config.json
```

FastReAct 仓库根目录 `./start.sh` 的查找顺序：

1. 显式路径：`./start.sh /path/to/config.json`
2. `.fastreact/config.json`
3. `~/.fastreact/config.json`
4. `fastreact-nano/.fastreact/config.json`
5. `fastreact-nano/config.json`

直接运行 backend 时，必须显式传 `--config`，否则核心库会先找 `~/.fastreact/config.json`：

```bash
python3 -m fastreact.adapters.http --config .fastreact/config.json
```

PSKA 的 `scripts/fastreact-pska-service-config` 用来生成 FastReAct 配置，默认输出仍是 `~/.fastreact/config.json`，兼容现有本机环境。新部署若采用仓库内配置，应显式传：

```bash
./scripts/fastreact-pska-service-config \
  --output "$FASTREACT_REPO/.fastreact/config.json"
```

### AuthNode

推荐运行配置：

```text
.authnode/config.json
```

生成来源：

```text
authnode.example.json
```

AuthNode `./start.sh` 默认读取 `.authnode/config.json`；旧环境如果已有 `authnode.local.json`，仍会兼容读取。新环境不存在配置时会从 example 生成 `.authnode/config.json`，并自动初始化本地 `jwt_secret` 和 `admin_token`。也可以用 `AUTHNODE_CONFIG` 显式覆盖。

## 兼容字段和不再推荐项

| 系统 | 字段/文件 | 状态 | 处理建议 |
| --- | --- | --- | --- |
| PSKA | `fastreact` | 兼容字段 | 仍被部分 CLI/local-daemon 使用；保持和 `agentic_service` 同 URL/token |
| PSKA | `agentic_service` | 当前 agentic 抽象 | Ask API 使用它构建 agentic service client |
| PSKA | `core/config.example.toml` | legacy 示例 | 不作为 runtime 配置；暂不删除，避免影响隐私检查测试 |
| PSKA | `.pska/config.toml` | 本机 legacy 文件 | 不参与当前 service/gateway 启动 |
| FastReAct | `gateway_workspace` | legacy 兼容字段 | runtime/store/session 仍会读取，暂不删除 |
| FastReAct | `FASTRACT_*` env | 历史前缀 | LLM/react/tool 等仍支持；新 service/auth/workspace 优先使用 `FASTREACT_*` |
| FastReAct | `mcp._fallback_stdio_mcp` | 示例/备用块 | `MCPConfig` 只读取 `mcp.servers`；不要指望它自动生效 |
| AuthNode | `authnode.local.json` | legacy 本机配置 | 仍兼容读取；新环境使用 `.authnode/config.json` |
| AuthNode | `authnode.example.json` | 模板 | 不放真实 secret；本地 secret 只在 `.authnode/config.json` 或兼容旧文件 |

## 新环境检查清单

1. AuthNode `.authnode/config.json` 的 `targets.fastreact.base_url` 是 `http://127.0.0.1:18741`，`targets.pska.base_url` 是 `http://127.0.0.1:8765`。
2. PSKA `.pska/config.json` 的 `startup.frontend.mode` 是 `gateway`，`fastreact.url` 和 `agentic_service.url` 都是 `http://127.0.0.1:18741`。
3. FastReAct 实际启动使用的 config 中 `service.port` 是 `18741`，MCP `servers[].url` 指向 `http://127.0.0.1:8765/mcp`。
4. PSKA gateway 需要稳定 session 时设置 `PSKA_GATEWAY_SESSION_SECRET`；本地不设也能启动，但重启会使旧 session 失效。
5. Docker/LAN 部署中，`PSKA_GATEWAY_AUTHNODE_BROWSER_URL` 指向浏览器可访问的 AuthNode URL；不要让浏览器重定向到 `http://authnode:8788` 这类容器内地址。
6. 不要把 `.authnode/config.json`、兼容旧文件 `authnode.local.json`、`.pska/config.json`、`.fastreact/config.json`、`~/.fastreact/config.json` 或 `~/.fastreact/credentials.json` 提交进仓库。
