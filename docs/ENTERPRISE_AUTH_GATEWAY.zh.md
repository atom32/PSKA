# PSKA 企业认证网关

本文描述 PSKA 在 AuthNode/FastReAct 统一多租户身份体系下的正规访问形态。

## 目标

浏览器不直接持有 AuthNode admin token、PSKA service token 或 FastReAct token。生产环境里，PSKA 进程也不应该依赖 AuthNode admin token 启动。PSKA 只应该知道验证身份所需的配置，例如 JWT issuer/audience/signing secret 或受信任 header 边界。

生产形态通常是：

```mermaid
sequenceDiagram
  participant Browser as Browser
  participant Gateway as Gateway/BFF or Ingress
  participant AuthNode as AuthNode
  participant PSKA as PSKA API
  participant FastReAct as FastReAct

  Browser->>Gateway: GET /
  Gateway-->>Browser: redirect to AuthNode/SSO when no session
  Browser->>AuthNode: interactive login / SSO
  AuthNode-->>Gateway: callback with one-time code
  Gateway->>AuthNode: POST /v1/auth/exchange
  AuthNode-->>Gateway: aud=pska JWT + claims
  Gateway-->>Browser: HttpOnly signed session cookie
  Browser->>Gateway: GET /workspace/today/data
  Gateway->>PSKA: forward AuthNode-verified JWT or trusted headers
  PSKA-->>Gateway: tenant-filtered data
  Gateway-->>Browser: JSON
  PSKA->>FastReAct: AuthNode-issued aud=fastreact token when agentic work is needed
  FastReAct->>PSKA: tenant/user-aware MCP calls
```

PSKA gateway 的 `/login` 默认重定向到 AuthNode 浏览器登录。AuthNode 登录后只把短期一次性 code 放回浏览器回调；PSKA gateway 在服务端调用 `POST /v1/auth/exchange` 换取 `aud=pska` JWT 和 claims，然后写入 HttpOnly session。PSKA 不提供密码系统、注册 UI 或组织管理后台。

## 启动

先启动 AuthNode、PSKA 后端和可选 FastReAct。三个服务仍然独立启动，不互相读取对方仓库里的本地配置文件。

PSKA 后端建议使用 JWT 模式：

```bash
export PSKA_AUTH_MODE=jwt
export AUTHNODE_JWT_SECRET='<same-secret-as-authnode>'
export PSKA_AUTH_JWT_SECRET="$AUTHNODE_JWT_SECRET"
export PSKA_AUTH_JWT_ISSUER=authnode.local
export PSKA_AUTH_JWT_AUDIENCE=pska
./start.sh
```

生产入口可以由企业 ingress/AuthNode proxy 承接，也可以由一个只处理已验证 SSO callback/code 的 BFF 承接。不要在生产中用 AuthNode admin token 让 PSKA gateway 代用户签发身份。

## 本地 AuthNode 登录

本地 smoke 使用 AuthNode 的 browser login + one-time code flow，不需要把 AuthNode admin token 给 PSKA。AuthNode、PSKA、FastReAct 仍各自由自己的启动脚本启动；PSKA 不启动其他项目。

先启动 AuthNode：

```bash
cd /Users/xudawei/Documents/AuthNode
./start.sh
```

再启动 PSKA：

```bash
cd /Users/xudawei/Documents/personal\ archive
export AUTHNODE_URL=http://127.0.0.1:8788
export PSKA_GATEWAY_SESSION_SECRET='<random-long-secret>'
./start.sh
```

打开：

```text
http://127.0.0.1:5173/
```

让 `./start.sh` 把常用前端端口 `5173` 交给 gateway。配置
`.pska/config.json`：

```json
"startup": {
  "frontend": {
    "enabled": true,
    "mode": "gateway",
    "host": "0.0.0.0",
    "port": 5173
  }
}
```

这样打开 `http://127.0.0.1:5173/` 或 LAN 地址上的 `:5173` 时，未登录会由
gateway 自动跳到 AuthNode `/login`。AuthNode 登录后回到 PSKA `/auth/callback`，
PSKA gateway 交换 code 并设置 HttpOnly session。如果使用 `"mode": "vite"`，
`5173` 是开发热更新服务器，不负责登录跳转。

旧的本地 token-broker 表单仍可通过 `/login?local=1` 使用，并且需要
`AUTHNODE_ADMIN_TOKEN` 或 `PSKA_GATEWAY_AUTHNODE_ADMIN_TOKEN`。它只用于调试
AuthNode `/v1/token`，不推荐作为日常登录路径。

如果后端仍运行在旧的 `service_token` 模式，gateway 可以临时使用服务端 token 代理：

```bash
export PSKA_GATEWAY_PSKA_SERVICE_TOKEN='<pska-service-token>'
```

这只是兼容路径；企业部署应优先使用 `PSKA_AUTH_MODE=jwt` 或受信任网关下的 `trusted_headers`。

## 浏览器行为

- 未登录访问 `/` 时，gateway 跳到 `/login`。
- 登录后，gateway 设置 `pska_gateway_session` HttpOnly cookie。
- 前端启动时调用 `/auth/session` 读取 tenant/user 摘要，只用于显示和构造本地 UI payload。
- 前端 API 调用仍使用相对路径，例如 `/workspace/today/data`。
- gateway 会丢弃浏览器传来的 `Authorization`、`X-PSKA-*`、`X-FastReAct-*`、`X-AuthNode-*` 和 cookie，再注入 gateway session 中的真实身份。

因此用户不需要手动把 token 填到前端。裸 `http://127.0.0.1:5173` 仍可用于开发热更新，但正规入口应是 gateway 或上游 ingress 暴露的地址。

Ask PSKA 同样走这个边界：浏览器只带 HttpOnly session cookie；gateway/BFF 注入
AuthNode 已验证的 tenant/user 身份给 PSKA。PSKA 调 FastReAct 时使用服务端配置的
FastReAct 凭据和 AuthNode/tenant 上下文，浏览器不接触 FastReAct service token。

## 关键环境变量

| 变量 | 用途 |
| --- | --- |
| `PSKA_GATEWAY_HOST` / `PSKA_GATEWAY_PORT` | Gateway 监听地址 |
| `PSKA_GATEWAY_FRONTEND_DIST` | 已构建前端目录，默认 `frontend/dist` |
| `PSKA_GATEWAY_PSKA_URL` | PSKA 后端 URL，默认 `http://127.0.0.1:8765` |
| `PSKA_GATEWAY_AUTHNODE_URL` / `AUTHNODE_URL` | AuthNode URL |
| `PSKA_GATEWAY_AUTHNODE_ADMIN_TOKEN` / `AUTHNODE_ADMIN_TOKEN` | 仅本地 token-broker 使用；生产不应要求 PSKA 持有 |
| `PSKA_GATEWAY_AUTHNODE_BROWSER_LOGIN` | 是否把 `/login` 重定向到 AuthNode browser login，默认 `true` |
| `PSKA_GATEWAY_SESSION_SECRET` | 签名浏览器 session cookie；生产必须设置 |
| `PSKA_GATEWAY_COOKIE_SECURE` | HTTPS 下设置为 `true` |
| `PSKA_GATEWAY_TOKEN_TTL_SECONDS` | AuthNode 签发的 PSKA JWT TTL |
| `PSKA_GATEWAY_PSKA_SERVICE_TOKEN` | 仅用于兼容旧 PSKA service-token 模式 |

## 安全边界

- AuthNode 只证明用户与租户身份；PSKA 仍负责知识 ACL、tenant filter、review 写入治理。
- Gateway 不是身份源，不存密码，不做组织管理。
- 强多租户要求 PSKA 后端运行在 `PSKA_AUTH_MODE=jwt` 或受保护 ingress 下的 `trusted_headers`，不能把公网用户可达的 `service_token + X-PSKA-*` headers 当作租户认证。
- `/login?local=1` token-broker 只能做 smoke test；它不能替代用户交互式登录，也不能作为生产租户边界。
- Browser 不能直接拿 AuthNode admin token、PSKA service token 或 FastReAct service token。
- Gateway 的 `/auth/session` 只返回 tenant/user/roles/groups 等摘要，不返回 JWT。
- PSKA 不能信任公网直连传入的 `X-PSKA-*` 头；这些头只在 AuthNode/gateway/loopback 受信任边界内有效。
- 上线前建议在共享 schema + `tenant_id` SQL 过滤之外，再加 Postgres RLS 作为第二道防线。

## 验证

```bash
curl -i http://127.0.0.1:5173/auth/session
```

未登录应返回 `401`。

登录后：

```bash
curl -b cookie.txt -c cookie.txt http://127.0.0.1:5173/auth/session
curl -b cookie.txt http://127.0.0.1:5173/workspace/today/data?owner_user_id=user_primary
```

期望 `/auth/session` 返回 `tenant_id` 和 `user_id` 摘要，workspace API 返回当前 tenant 范围内的数据。
