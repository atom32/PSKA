# PSKA

PSKA 是一个 private-first 的多租户本地知识工作台。它把按 tenant/user 隔离的
资料接入系统，沉淀为 documents、chunks、digest notes、review candidates、
memories、graph relationships、citations 和 writing evidence。PSKA 不只是
一个 RAG 聊天框；它的核心产品链路是：
`Digest -> Candidates -> Review -> Discovery -> Graph/Memory -> Writing`。

英文版本见 [README.md](README.md)。

## 从这里开始

- [中文文档索引](docs/README.zh.md)
- [Documentation Index](docs/README.md)
- [Developer Quickstart](docs/DEVELOPER_QUICKSTART.md)
- [企业认证网关](docs/ENTERPRISE_AUTH_GATEWAY.zh.md)
- [多租户 Workspace E2E](docs/MULTITENANT_WORKSPACE_E2E.zh.md)

## 多租户版本一眼看懂

- 分支策略：多租户/AuthNode/FastReAct 集成工作提交到 `tenant` 分支；除非 owner 明确要求，不要把 tenant 线合回 `master`。
- 身份模型：每个请求都必须落到 `tenant_id`、`user_id`，以及可选的 `represented_user_id`。
- 浏览器认证：正常多租户浏览器访问走 PSKA Gateway + AuthNode；浏览器只应该持有 HttpOnly gateway session cookie。
- Agentic 能力：Deep Ask 和 digest generation 由 FastReAct 执行；PSKA 负责 readiness、MCP 边界、引用、review 治理和 tenant 可见性。
- 核心覆盖：Knowledge Sources、processing spans、chunk preview、Digest、Review、带证据的 Ask、Evidence Briefs/Writing、readiness diagnostics 都是 tenant 版本的一等能力。

## 本地服务

| 服务 | 默认地址 | 启动位置 | 职责 |
| --- | --- | --- | --- |
| AuthNode | `http://127.0.0.1:8788` | AuthNode 仓库 `./start.sh` | 登录、tenant/user claims、本地 IAM 或 OIDC |
| PSKA API | `http://127.0.0.1:8765` | 本仓库 `./start.sh` | 知识库、source sync、review、Ask、MCP |
| PSKA Gateway/UI | `http://127.0.0.1:5173` | 本仓库 `./start.sh` | 浏览器入口和前端 |
| FastReAct | `http://127.0.0.1:8000` | FastReAct 仓库启动命令 | Agentic Ask/digest 执行 |

PSKA 不会替你启动 AuthNode 或 FastReAct。先在各自项目里启动它们，再回到本仓库执行 `./start.sh`。验证 PSKA 时应使用集成启动路径 `./start.sh`，除非你明确在做单独进程调试。

## 首次配置

```bash
brew install python@3.12
./scripts/bootstrap_pska_env
mkdir -p .pska
cp core/config.pska.example.json .pska/config.json
./scripts/pska --config .pska/config.json db-check
./scripts/pska --config .pska/config.json db-create --name pska
./scripts/pska --config .pska/config.json db-init
cd frontend
npm install
```

`.pska/config.json` 是本机配置，不要提交。它可能包含本地路径、模型 key、service token 和 FastReAct token。

多租户浏览器登录测试建议把前端模式设为 `gateway`：

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

然后启动 AuthNode 和 PSKA：

```bash
cd /Users/xudawei/Documents/AuthNode
./start.sh

cd /Users/xudawei/Documents/personal\ archive
export AUTHNODE_URL=http://127.0.0.1:8788
export PSKA_GATEWAY_SESSION_SECRET='<random-long-secret>'
./start.sh
```

打开：

```text
http://127.0.0.1:5173/
```

只有在明确需要前端热更新时才使用 `startup.frontend.mode: "vite"`；Vite 本地开发会用浏览器 `sessionStorage` 保存轻量身份上下文，不是正式登录边界。

## 日常开发流程

启动依赖：

```bash
cd /Users/xudawei/Documents/AuthNode
./start.sh

cd /Users/xudawei/FastReAct
./start.sh

cd /Users/xudawei/Documents/personal\ archive
./start.sh
```

检查服务：

```bash
curl -s http://127.0.0.1:8788/health
curl -s http://127.0.0.1:8765/health
curl -s http://127.0.0.1:8000/health
./scripts/pska --config .pska/config.json service-check
```

如果 `.pska/config.json` 设置了 `service.service_token`，直连 API 时需要带 `Authorization: Bearer <token>` 或 `X-PSKA-Service-Token: <token>`。

## Tenant 数据目录

运行时数据默认在：

```text
~/PSKA_workspaces
```

多租户版本把系统文件和用户内容分开：

```text
~/PSKA_workspaces/_system/run
~/PSKA_workspaces/_system/logs
~/PSKA_workspaces/tenants/<tenant_id>/users/<user_id>/sources
```

`./start.sh` 只准备系统目录，不会静默摄入用户资料。用户资料必须显式添加为 tenant/user source。CLI 目前提供 folder 快捷入口：

```bash
mkdir -p "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/sources"

./scripts/pska --config .pska/config.json knowledge-source add-folder \
  --tenant-id tenant_default \
  --owner-user-id user_primary \
  --space-id private_primary \
  --path "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/sources"

./scripts/pska --config .pska/config.json files-sync \
  --tenant-id tenant_default \
  --owner-user-id user_primary \
  --root "$HOME/PSKA_workspaces/tenants/tenant_default/users/user_primary/sources"
```

Workspace UI 和 HTTP API 支持 folder、RSS/Atom、URL source 的 preview、sync、cleanup、retry、processing spans 和 chunk preview。

## Digest、Review、Ask、Writing

Digest 是 PSKA 的差异化能力。一次 digest 应该产出带 source_refs 的 `digest_note`、`knowledge_claim`、`review_item`、`memory_candidate`、`relationship_candidate` 等治理对象。长期知识写入必须保留 `source_refs`；低置信或高影响变更应进入 Review，不能直接写长期 Memory/Graph。

常用命令：

```bash
./scripts/pska --config .pska/config.json digest-now
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json daily-briefing --owner-user-id user_primary --limit 5
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

Ask PSKA 支持 direct 和 FastReAct-backed 两类路径。回答需要展示 citations、source/chunk preview、progress、evidence check；当没有可见证据时，要明确解释为什么没找到答案，而不是假装成功。

Evidence Briefs 是 PSKA 风格的 Wiki 路径：digest notes、Ask results、reviewed claims 可以生成带 citations、source_refs、lineage 和 review 状态的 Writing board 草稿。PSKA 不自动发布未经 review 的 Wiki 页面。

## API 速查

在可信本地边界内做 API/CLI 测试时，显式传入身份：

```bash
TOKEN='<service.service_token>'
curl -s http://127.0.0.1:8765/workspace/readiness \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-PSKA-Tenant-Id: tenant_default" \
  -H "X-PSKA-User-Id: user_primary" \
  -H "X-PSKA-Represented-User-Id: user_primary" \
  -H "X-PSKA-Subject: pska:user_primary"
```

如果本地 service auth 关闭，可以省略 `Authorization`。

常用 Workspace endpoint：

- `GET /workspace/readiness`
- `GET /console/sources/data`
- `GET /workspace/sources/adapters`
- `POST /workspace/sources/preview`
- `POST /workspace/sources`
- `POST /workspace/sources/sync`
- `POST /workspace/chunking/preview`
- `GET /workspace/digest/data`
- `POST /workspace/digest/run`
- `POST /workspace/ask`
- `POST /workspace/ask/stream`
- `POST /workspace/evidence-briefs`

浏览器/SaaS 场景不要把 service token 或原始 tenant headers 暴露给用户。应把前端放在 PSKA Gateway/AuthNode、JWT 模式或可信 ingress 后面，由服务端注入已验证身份。

## 验证

推荐本地检查：

```bash
./start.sh
./scripts/pska --config .pska/config.json service-check
PYTHONPATH=core/src python -m pytest core/tests/test_fastreact_integration.py
PYTHONPATH=core/src python -m pytest core/tests
cd frontend
npm run build
```

完整多租户 Writing smoke：

```bash
./scripts/pska-writing-workspace-e2e --config ".pska/config.json"
```

## 工程约束

- PSKA 必须保持 domain-agnostic，不要为了样例语料、样例公司或特定问题写硬编码捷径。
- 回答质量提升要做成通用 retrieval、digest、review、citation、writing 机制，并配跨领域测试。
- imports、run files、logs、active workspace data、本地凭据都不要放进源码仓库。

## 主要组件

- `core/`：tenant-aware 知识模型、source adapters、ingestion、jobs、search、digest、review、Ask、Evidence Briefs、HTTP API、local daemon 和 MCP 工具。
- `frontend/`：User Workspace，包含 Today、Discoveries、Knowledge Sources、Corpus/Brain、Ask、Graph、Review-oriented flows 和 Writing/Evidence Brief surfaces。
- `channels/twitter-x/`：Twitter/X 归档采集器和 archive schema。

## 许可证

MIT
