# 里程碑：Phase 1 多知识库 RAG 底座

本里程碑对应 `PSKA_PRODUCT_REDESIGN_PLAN.zh.md` 的 Phase 1，也是 `PSKA_MULTI_KB_PRODUCT_PLAN.zh.md` 的第一条工程落地线。目标不是一次性重做所有前端，而是先把 PSKA 从“一个用户一组资料”升级为“一个账号多个可检索知识库”的后端与产品边界。

## 范围

本阶段必须完成：

- `KnowledgeBase` 成为一等数据对象。
- 每个 `(tenant_id, owner_user_id)` 有默认知识库。
- 现有资料可以归入默认知识库。
- 知识库可以创建、读取、列出、更新、归档。
- source / source_item membership 有稳定表和 store API。
- 后续 Ask 可以把 `knowledge_base_ids` 解析成 `source_item_ids`。
- API 响应能展示知识库 counts/readiness 的最小版本。

本阶段不做：

- 大规模前端重构。
- Graph/Writing 产品重排。
- 公开知识库广场。
- 完整共享/RBAC。
- 跨 embedding model 的复杂融合。

## 验收清单

### 数据与迁移

- 新增 `knowledge_bases`。
- 新增 `knowledge_base_sources`。
- 新增 `knowledge_base_source_items`。
- migration 幂等。
- 迁移会为已有用户创建 default KB。
- 迁移会把已有 `knowledge_sources` 和 `source_items` 加入 default KB membership。

### Store

- in-memory store 与 Postgres store 都支持 KB CRUD。
- default KB 创建是幂等的。
- source membership 和 source_item membership 可写入、可查询。
- tenant/user 隔离不会串数据。
- `count_table("knowledge_bases")` 可用。

### API

- `GET /workspace/knowledge-bases` 返回当前用户 KB 列表。
- `POST /workspace/knowledge-bases` 创建 KB。
- `GET /workspace/knowledge-bases/{id}` 返回详情。
- `PATCH /workspace/knowledge-bases/{id}` 更新名称、描述、状态、配置等。
- `DELETE /workspace/knowledge-bases/{id}` 归档 KB。
- 未传 KB 的旧资料流继续可用。

### RAG 准备

- 能从 KB membership 得到 active `source_item_ids`。
- 后续 `scope.knowledge_base_ids` 可以不改 retrieval 主体，先编译成现有 `source_item_ids` hard scope。
- 不允许 KB scope 绕过 ACL。

### 测试

- 创建多个 KB 并列出。
- default KB 幂等。
- membership 查询只返回对应 KB 的 source items。
- 不同 tenant/user 的 KB 不互相可见。
- API 基础 CRUD 通过 in-memory 测试。

## 第一阶段交付顺序

1. 模型、迁移、store。
2. API CRUD。
3. source/text/upload 绑定 `knowledge_base_id`。
4. Ask scope resolution。
5. 前端 scope chip 和资料库 KB rail。

## 当前进度

截至当前实现，1-5 已进入可运行状态：

- 模型、migration、in-memory store、Postgres store 已有 `knowledge_bases`、`knowledge_base_sources`、`knowledge_base_source_items`。
- API 已覆盖 KB CRUD、置顶、归档、恢复、documents membership 删除、加入和移动。
- text/upload/source/sync 路径已能绑定 `knowledge_base_id`。
- Ask、KB search、MCP read tools 已支持 `knowledge_base_ids -> source_item_ids` 的 hard scope 解析。
- 前端已有 KB rail、scope chip、KB 管理、归档恢复、证据搜索、KB readiness 健康面板、资料加入/移动/移除，以及资料卡 KB badge。

新增验收证据：

- 旧资料迁移：`core/tests/test_knowledge_base_migration.py` 会在临时 Postgres 库中先跑 001-020 migration，写入 legacy tenant/user/source/source_item，再运行 `021_knowledge_bases.sql` 两次，确认 default KB backfill、source/source_item membership 和幂等性。
- scoped Ask：`frontend/e2e/multi-kb-scoped-ask.spec.ts` 使用 AuthNode/Gateway 浏览器 session 创建两个临时知识库，写入互斥证据，调用 hard `knowledge_base_ids` scope 的 Ask，并断言 citation/source_ref/source_window 不包含未选 KB。2026-07-04 复跑 `npm run e2e:multi-kb-ask` 通过，且 `PSKA_MULTI_KB_SCOPE_%` 临时数据 residue 计数为 `0,0,0,0,0,0`。
- KB readiness UI：资料库页基于真实 `counts/readiness` 展示处理状态、资料/原文/片段数、embedding coverage、embedding model、offline index freshness、最近同步和最近 Digest；`frontend/e2e/multi-kb-scoped-ask.spec.ts` 会进入临时 KB 的资料库页并断言 readiness 面板可见。
- Ask scope readiness：`route.scope_applied` 会返回选中知识库的 compact `knowledge_base_readiness` 与 `knowledge_base_readiness_warnings`；Ask 结果头部展示“范围可检索/待检查”，no-answer diagnostics 会说明空库、无 chunks、处理失败等通用原因。
- 资料 membership UI：手工 smoke 已覆盖同一资料 link 到第二 KB、按当前 KB 删除 membership、切回原 KB 后资料仍保留，并修复了 KB 切换时删除 preview 残留的问题。
- API contract：`docs/API_REFERENCE.md` 已补 KB CRUD、ingest/list/search scope、Ask `route.scope_applied`、deep `tool_policy.scope`、documents membership delete/link/move、MCP KB scope 语义。
- dropped scope：hard KB scope 与显式 `source_item_ids` 做交集时，`route.scope_applied.dropped_scope_ids` / `dropped_source_item_ids` 会记录被 KB membership 剔除的显式 source ids；不可访问 KB 仍 fail-closed，不泄露 id。
- 真实 FastReAct 验收：2026-07-04 使用 `tenant_graphintell / test_user`、真实 `~/FastReAct` daemon、DeepSeek API、PSKA HTTP MCP 跑通 deep Ask。测试创建 alpha/beta 两个临时 KB，Ask hard scope 只选择 alpha；FastReAct 实际 `pska_pska_search` / `pska_pska_read_evidence_context` / `pska_pska_graph_context` tool call 均带 `knowledge_base_ids=[alpha]`、`source_item_ids=[alpha source]`、`scope_mode=hard`，metadata 显示 `tool_policy_scope_applied=true`；答案和 trace 均未出现 beta secret 或 beta KB id。
- 可复跑脚本：`scripts/pska-fastreact-kb-scope-smoke` 会使用真实 PSKA service、真实 FastReAct daemon、真实 Deep Ask、真实 PSKA HTTP MCP，自动创建 alpha/beta KB、校验 tool-call scope、并清理临时数据。2026-07-04 复跑输出 `ok=true`，FastReAct model 为 `deepseek-v4-flash`，PSKA MCP `tool_count=10`。
- 真实测试清理：上述 smoke 通过 API hard delete + 精确 SQL residue check 清理临时数据，`knowledge_bases, knowledge_sources, source_items, documents, chunks, passage_windows` 残留计数为 `0,0,0,0,0,0`。
- 发布门禁脚本：`scripts/pska-phase1-multikb-release-gate` 默认跑非 LLM/非浏览器检查；`--include-browser-e2e` 和 `--include-fastreact-smoke` 才进入真实 UI 或真实 DeepSeek/FastReAct/PSKA MCP 链路。
- 默认发布门禁：2026-07-04 复跑 `./scripts/pska-phase1-multikb-release-gate` 通过，覆盖 PSKA core `490 passed`、frontend build、FastReAct contracts `75 passed`、PSKA/FastReAct `git diff --check`。

发布说明已整理到 `docs/RELEASE_PHASE1_MULTI_KB_RAG.zh.md`。下一步优先按需复跑真实 UI / 真实 FastReAct smoke，并准备提交/PR。
