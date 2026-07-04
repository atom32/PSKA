# Release Notes: Phase 1 多知识库 RAG 底座

Status: release candidate on `tenant` branch
Date: 2026-07-04
Primary milestone: `docs/MILESTONE_PHASE1_MULTI_KB_RAG.zh.md`

## 发布目标

本次发布把 PSKA 从“一个账号一组资料”升级为“一个账号可以管理多个知识库，并把 Ask/RAG 范围限定到选中的知识库”。它是后续 Digest、Review、Graph、Writing 继续产品化的证据边界底座。

本阶段把多知识库作为 corpus 边界，而不是权限捷径。所有检索仍然必须执行 tenant/user ACL；KB scope 只决定候选集合，不能扩大用户原本不可见的资料。

## 用户可见变化

- 资料库增加 KB rail、scope chip、KB 创建/置顶/归档/恢复入口。
- 上传、粘贴文本、source/sync 可以绑定当前或指定 `knowledge_base_id`。
- 资料列表展示 KB badge，并支持加入、移动、从当前 KB 移除 membership。
- Ask 可以使用当前 KB 或多 KB scope；结果展示 `scope_applied`、KB lineage、no-answer diagnostics。
- Deep Ask 通过 FastReAct 调 PSKA MCP 时保留同一 KB hard scope，不再出现 quick scoped / deep unscoped 的产品不一致。
- Digest、Review、Graph、Writing 开始保留或接收 KB scope/lineage，作为后续产品层扩展基础。

## 后端/API 变化

- 新增 migration `021_knowledge_bases.sql`：
  - `knowledge_bases`
  - `knowledge_base_sources`
  - `knowledge_base_source_items`
- 每个 `(tenant_id, owner_user_id)` 会有幂等 default KB。
- 旧 `knowledge_sources` / `source_items` 会 backfill 到 default KB membership。
- in-memory store 与 Postgres store 支持 KB CRUD、default KB、source/source_item membership、pin/archive/restore。
- Workspace API 增加：
  - `GET/POST /workspace/knowledge-bases`
  - `GET/PATCH/DELETE /workspace/knowledge-bases/{id}`
  - `POST /workspace/knowledge-bases/{id}/restore`
  - `POST/DELETE /workspace/knowledge-bases/{id}/pin`
  - documents membership delete/link/move
  - KB-scoped search and Ask
- Ask `route.scope_applied` 成为 canonical resolved scope，包含 `knowledge_base_ids`、`source_item_ids`、counts、`dropped_scope_ids`、`dropped_source_item_ids`。
- PSKA read-only MCP tools 接受 top-level 或 nested `scope` 中的 `knowledge_base_ids` / `source_item_ids` / `scope_mode`。
- Deep Ask public trace 保留安全 scope audit 字段：tool-call `knowledge_base_ids`、`source_item_ids`、`scope_mode`、`metadata.tool_policy_scope_applied`。

## FastReAct 协议变化

FastReAct `tool_policy` 现在保留 `scope`，并在执行 PSKA MCP tool call 前注入：

```json
{
  "mode": "hard",
  "scope_mode": "hard",
  "knowledge_base_ids": ["kb_..."],
  "source_item_ids": ["src_..."]
}
```

如果模型自己传了 `source_item_ids`，运行时会与 policy `source_item_ids` 做交集。FastReAct 事件 metadata 会记录 `tool_policy_scope_applied=true` 和安全化后的 `tool_policy.scope`。PSKA 仍在每次 MCP 调用中重新校验 tenant/user ACL 和 KB access。

## 不在本阶段发布

- 公开知识库广场。
- 完整 team/shared/RBAC 产品化。
- per-KB embedding/chunking/parser 配置和自动 reparse/backfill。
- 跨 embedding model 的复杂融合。
- ResearchTask 表、任务队列和完整 Deep Research 产品页。
- 大规模前端 IA 重排。

## 验收命令

默认发布门禁不会启动浏览器，也不会调用付费 LLM。它会跑 PSKA core、frontend build、FastReAct contracts，以及 PSKA/FastReAct 两边的 `git diff --check`：

```bash
cd "$PSKA_REPO"
./scripts/pska-phase1-multikb-release-gate
```

真实 UI / 真实 LLM 链路需要显式开启：

```bash
cd "$PSKA_REPO"
./scripts/pska-phase1-multikb-release-gate --include-browser-e2e
./scripts/pska-phase1-multikb-release-gate --include-fastreact-smoke
```

也可以一次性开启所有真实链路：

```bash
cd "$PSKA_REPO"
./scripts/pska-phase1-multikb-release-gate --include-all-live
```

先用集成启动方式启动 PSKA：

```bash
cd "$PSKA_REPO"
./start.sh
```

核心回归：

```bash
cd "$PSKA_REPO/core"
PYTHONPATH=..:.:src pytest -q
```

前端构建：

```bash
cd "$PSKA_REPO/frontend"
npm run build
```

浏览器 scoped Ask 验收：

```bash
cd "$PSKA_REPO/frontend"
PSKA_E2E_TENANT_ID="tenant_graphintell" \
PSKA_E2E_USER_ID="test_user" \
PSKA_E2E_PASSWORD="<local AuthNode password>" \
  npm run e2e:multi-kb-ask
```

真实 FastReAct + PSKA HTTP MCP + DeepSeek KB scope 验收：

```bash
cd "$PSKA_REPO"
./scripts/fastreact-pska-service-config \
  --mcp-transport http \
  --output .pska/fastreact-pska-http.json

cd "$FASTREACT_NANO_REPO"
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src \
  python3 -m fastreact.adapters.http \
  --config "$PSKA_REPO/.pska/fastreact-pska-http.json"

cd "$PSKA_REPO"
./scripts/pska-fastreact-kb-scope-smoke
```

FastReAct 合同测试：

```bash
cd "$FASTREACT_NANO_REPO"
PYTHONPATH=src pytest tests/contracts/test_runtime_contracts.py tests/contracts/test_http_service_contract.py -q
```

## 当前验证证据

- Release gate default: `./scripts/pska-phase1-multikb-release-gate` passed in `33.491s`
- PSKA core: `490 passed`
- FastReAct contracts: `75 passed`
- Browser multi-KB Ask e2e: `npm run e2e:multi-kb-ask` passed with AuthNode/Gateway session for `tenant_graphintell / test_user`
- Browser e2e residue counts: `knowledge_bases=0, knowledge_sources=0, source_items=0, documents=0, chunks=0, passage_windows=0`
- `scripts/pska-fastreact-kb-scope-smoke`: `ok=true`
- Real smoke residue counts: `knowledge_bases=0, knowledge_sources=0, source_items=0, documents=0, chunks=0, passage_windows=0`
- Release gate dry-run: `./scripts/pska-phase1-multikb-release-gate --dry-run` planned default non-live checks without browser/LLM steps
- `git diff --check`: PSKA and FastReAct clean through the default release gate

## 运行和回滚注意事项

- 不要把 `.pska/fastreact-pska-http.json`、`~/.fastreact/credentials.json` 或 API key/token 提交到仓库。
- 如果 deep Ask 失败，先确认 FastReAct `/ready` 中 `mcp.ready=true` 且包含 `pska_pska_search`。
- 如果 KB scope 结果为空，检查 `route.scope_applied.source_item_count`、`dropped_scope_ids`、`selected_knowledge_base_empty` / `selected_scope_empty` diagnostics。
- 回滚产品入口时可以隐藏前端 KB rail/scope picker，但后端 migration 需要保留兼容旧资料；旧 API 不传 KB 时继续按 all/default visible corpus 工作。
