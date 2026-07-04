# PSKA 多知识库产品完善计划

本文把上一轮调研收敛成可执行的产品和工程计划。目标是让 PSKA 不只“能作为 RAG 使用”，而是具备清晰的多知识库产品心智、稳定的数据边界、可验证的检索质量，以及能承接 Digest / Review / Writing 差异化能力的长期架构。

## 1. 背景与结论

### 当前判断

PSKA 现在已经具备 RAG 核心链路：

- ingestion 生成 `source_items`、`documents`、`chunks`。
- `RetrievalService` 做 ACL 过滤、lexical/BM25、可选 vector retrieval、RRF 融合、GraphRAG 扩展、引用生成。
- `workspace_ask` 提供 quick/deep/auto，quick 由 PSKA 检索和 evidence check 兜底，deep 通过 FastReAct 使用 PSKA read-only MCP tools。
- Ask 响应包含 answer、citations、source_refs、source_windows、agent_steps、trace、no-answer diagnostics。
- hard scope 已经支持 `source_item_ids`，能作为多知识库改造第一阶段的检索基础。

但当前产品和数据库还不是“一账号多个知识库”模型：

- `KnowledgeSource` 是 source connector/config，不是知识库容器。
- `source_items`、`documents`、`chunks` 没有 `knowledge_base_id` 或稳定 membership 表。
- Ask conversation/run 没有保存知识库 scope。
- 现有 `space_id` 更像 ACL/协作空间，不适合作为知识库概念直接复用。
- 部分 source sync/upload 路径默认禁用 embedding，作为“可用 RAG”成立，但默认高质量 vector RAG 仍需补齐配置、回填和 readiness。

### 目标状态

PSKA 应形成三层产品模型：

```text
知识库 KnowledgeBase
  -> 资料/来源 SourceItem / KnowledgeSource / Document
    -> 检索单元 Chunk / PassageWindow / SourceRef
```

知识库是一等产品对象，承担：

- corpus 边界：哪些资料属于这个知识库。
- 检索边界：Ask 选择一个或多个知识库时只能在这些 corpus 中检索。
- 处理配置：chunking、embedding、parser、digest/review 策略的默认值。
- 可见性和协作：owner、private/team/tenant/shared/read-only。
- readiness：资料数、chunk 数、embedding 覆盖率、processing 状态、offline index freshness。

PSKA 的差异化不是照搬 WeKnora 的企业 RAG 平台，而是在多知识库 RAG 基础上继续连接：

```text
Ask -> Evidence -> Digest -> Review -> Graph/Memory -> Writing
```

也就是说，WeKnora 解决“问资料”，PSKA 还要解决“资料怎样沉淀成可审阅、可写作、可长期复用的知识”。

## 2. 产品原则

### P1. 知识库是 corpus，不是权限捷径

知识库 membership 只决定检索候选集合，不能绕过 item-level ACL。任何检索都必须执行：

```text
candidate_source_items = source_items_in_selected_kbs ∩ acl_visible_source_items
```

如果用户选择了自己无权访问的知识库，应返回明确错误或过滤后带 diagnostics，不能静默泄露 metadata。

### P2. KnowledgeSource 继续表示连接器，KnowledgeBase 表示产品容器

不要把 `knowledge_sources` 改名或扩义成知识库。它现在已经承载 folder/rss/url/text/upload-like source lifecycle、sync_runs、processing_spans。多知识库应该新增 `knowledge_bases` 和 membership 表，避免 source connector 与 corpus 容器混在一起。

### P3. 默认兼容单知识库心智

迁移后，每个 `(tenant_id, owner_user_id)` 自动有一个默认知识库。旧 API 不传 `knowledge_base_ids` 时继续表现为“我的资料库全部资料”，但响应里应逐步返回 `default_knowledge_base_id` 和 `scope_applied`，帮助前端迁移。

### P4. Ask scope 必须可解释

用户每次 Ask 都应该知道系统查了哪里：

- 全部可见知识库。
- 当前知识库。
- 选中的多个知识库。
- 指定资料/附件。
- Writing board context。

响应必须保留 `scope_applied`、`knowledge_base_ids`、`source_item_count`、`dropped_scope_ids`、`scope_mode`。

### P5. 质量能力必须 domain-agnostic

不能用样例公司、样例实体、固定问法或 benchmark shortcut 提升效果。检索质量改进只能来自通用机制：chunking、embedding、hybrid retrieval、query rewrite、rerank、evidence verification、no-answer policy、eval corpus。

## 3. 核心用户场景

### 场景 A：个人多知识库

用户有多个项目资料：

- “论文阅读”
- “产品调研”
- “客户访谈”
- “个人写作”

用户进入某个知识库，只看该知识库的资料、处理状态和问答历史；Ask 默认只查当前知识库，但可以切换到“全部”或选择多个知识库。

### 场景 B：一个账号管理多个来源集合

同一账号可以把 folder、RSS、URL、text/upload 分配到不同知识库。一个来源可以：

- 只属于一个知识库，默认行为。
- 被 copy 到另一个知识库并重新处理。
- 被 link 到多个知识库，共享原始 source_item，但检索 membership 不同。

初期建议只做“移动/复制资料到知识库”，不要默认允许一个 source 自动属于多个知识库，避免删除、同步、digest lineage 变复杂。

### 场景 C：跨知识库 Ask

用户在 Ask 里选择多个知识库。PSKA 应：

- 校验所有 KB 可访问。
- 校验 embedding/model/index 兼容性；不兼容时降级到 lexical-only 或提示用户分开检索。
- 返回每条 citation 所属 KB。
- 在 no-answer diagnostics 中说明是“选中知识库无证据”，还是“资料未处理/embedding 不完整/权限过滤后无候选”。

### 场景 D：Digest / Review / Writing 绑定知识库

用户对某个知识库运行 Digest，产出的 notes/claims/review candidates 应记录 KB lineage。用户把 Ask run 转成 Evidence Brief 或 Writing node 时，也应保留：

- ask_run_id
- selected knowledge_base_ids
- source_refs
- review status
- evidence_check status

这样后续可以按知识库查看“这个知识库沉淀出了哪些知识和草稿”。

### 场景 E：团队共享

团队成员可以看到 shared/read-only 知识库。MVP 可以先复用现有 tenant/team visibility，但产品对象上需要预留：

- `visibility`
- `visible_team_ids`
- `created_by_user_id`
- `owner_user_id`
- `permission_policy`

检索仍然必须 intersect item ACL。

## 4. 产品信息架构

### 第一层：知识库首页

入口：`/workspace/knowledge-bases` 或现有资料库入口升级。

列表信息：

- 名称、描述、类型：document / faq-like / mixed，初期可只支持 document。
- 所属：我创建、团队共享、只读。
- 资料数、chunk 数、embedding 覆盖率。
- processing 状态：idle / processing / failed / stale。
- 最近同步时间、最近 Ask 时间、最近 Digest 时间。
- pin/favorite、创建、设置、删除。

筛选：

- 全部
- 我创建
- 共享给我
- 最近使用
- 已置顶
- 处理异常

空状态：

- 首次使用：创建默认知识库并引导添加资料。
- 有知识库但无资料：显示 upload/add source 动作。
- 有资料但未处理：显示 processing/readiness。

### 第二层：知识库详情

详情页是主要工作台，建议分 tabs：

- `资料`：documents/source items 列表、上传、添加 URL/RSS/folder、移动/复制、删除/恢复。
- `处理`：sync runs、processing spans、chunk preview、embedding coverage、parse errors。
- `Ask`：当前 KB 默认 scope 的问答面板。
- `Digest`：该 KB 的 digest notes、claims、review candidates。
- `Graph/Memory`：该 KB 贡献的 entities/relationships/memories。
- `Writing`：从该 KB 证据生成的 briefs/boards。
- `设置`：名称、描述、默认 chunking/parser/embedding、可见性。

### 第三层：Ask scope selector

Ask 输入框旁提供 scope selector：

- 全部可见知识库
- 当前知识库
- 选择多个知识库
- 当前附件/指定资料

选择器行为参考 WeKnora：

- 可搜索。
- 显示资料数/chunk 数/ready 状态。
- 支持全选/清空。
- 不显示或禁用未初始化 KB，并说明原因。
- 最近使用和当前 KB 置顶。

### 第四层：资料移动/复制

资料列表支持：

- `Move to knowledge base`：从源 KB 移到目标 KB。
- `Copy to knowledge base`：复制 membership 或重新 ingest。
- `Reparse in target KB`：目标 KB chunking/parser 不同时重新处理。
- `Reuse vectors`：目标 KB embedding config 一致时复用 chunk/vector。

MVP 只需要：

- 同一 tenant/user 下 move。
- 相同 embedding/chunking 配置时 reuse。
- 不同配置时要求 reparse。

### 第五层：系统 readiness

知识库详情和全局 readiness 都应展示：

- `source_item_count`
- `document_count`
- `chunk_count`
- `active_chunk_count`
- `embedding_coverage`
- `embedding_provider/model`
- `offline_index_freshness`
- `processing_count`
- `failed_processing_count`
- `last_sync_at`
- `last_digest_at`

Ask 如果选中的 KB readiness 不满足，应在 route/evidence diagnostics 中体现。

## 5. 数据模型计划

### 阶段 1：新增一等 KB 与 membership

新增 migration，例如 `021_knowledge_bases.sql`。

建议表：

```sql
CREATE TABLE knowledge_bases (
  knowledge_base_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  owner_user_id text NOT NULL,
  created_by_user_id text NOT NULL,
  slug text NOT NULL,
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  kb_type text NOT NULL DEFAULT 'document',
  status text NOT NULL DEFAULT 'active',
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  default_space_id text REFERENCES spaces(space_id) ON DELETE RESTRICT,
  is_default boolean NOT NULL DEFAULT false,
  pinned_at timestamptz,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  readiness jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  UNIQUE (tenant_id, owner_user_id, slug)
);

CREATE TABLE knowledge_base_sources (
  knowledge_base_id text NOT NULL REFERENCES knowledge_bases(knowledge_base_id) ON DELETE CASCADE,
  knowledge_source_id text NOT NULL REFERENCES knowledge_sources(knowledge_source_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  owner_user_id text NOT NULL,
  membership_status text NOT NULL DEFAULT 'active',
  added_by_user_id text NOT NULL,
  added_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (knowledge_base_id, knowledge_source_id)
);

CREATE TABLE knowledge_base_source_items (
  knowledge_base_id text NOT NULL REFERENCES knowledge_bases(knowledge_base_id) ON DELETE CASCADE,
  source_item_id text NOT NULL REFERENCES source_items(source_item_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  owner_user_id text NOT NULL,
  membership_type text NOT NULL DEFAULT 'manual',
  membership_status text NOT NULL DEFAULT 'active',
  added_by_user_id text NOT NULL,
  added_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (knowledge_base_id, source_item_id)
);
```

索引：

```sql
CREATE INDEX knowledge_bases_owner_idx
  ON knowledge_bases(tenant_id, owner_user_id, status, updated_at DESC);

CREATE INDEX knowledge_bases_visibility_idx
  ON knowledge_bases(tenant_id, visibility, status);

CREATE INDEX knowledge_base_source_items_source_idx
  ON knowledge_base_source_items(tenant_id, source_item_id, membership_status);

CREATE INDEX knowledge_base_source_items_kb_idx
  ON knowledge_base_source_items(tenant_id, knowledge_base_id, membership_status);
```

说明：

- `knowledge_base_sources` 绑定 connector/config 来源。
- `knowledge_base_source_items` 绑定实际检索 corpus。
- 同步一个 folder/rss/url 后，应把本次生成/更新的 source_items 写入所属 KB membership。
- source membership 和 source_item membership 都需要，因为“来源配置”和“实际文档集合”生命周期不同。

### 阶段 2：下沉 KB id 以优化检索

当第一阶段稳定后，在以下表增加可选字段或派生表：

- `source_items.primary_knowledge_base_id`
- `documents.primary_knowledge_base_id`
- `chunks.primary_knowledge_base_id`
- 或建立 materialized mapping：`knowledge_base_chunks(knowledge_base_id, chunk_id, source_item_id, tenant_id, lifecycle_status)`

推荐先用 mapping，不急于强制单归属。原因：

- 同一资料未来可能被多个 KB 复用。
- hard delete / restore / reparse 更容易保持幂等。
- 检索过滤可以直接 join mapping，而不破坏现有 source_item-based API。

### 阶段 3：Ask/Digest/Review/Writing lineage

新增字段：

- `ask_conversations.default_knowledge_base_ids jsonb`
- `ask_runs.scope jsonb`
- `ask_runs.knowledge_base_ids text[]`
- `digest_notes.knowledge_base_ids text[]`
- `knowledge_claims.knowledge_base_ids text[]`
- `review_items.knowledge_base_ids text[]`
- `writing_boards.knowledge_base_ids text[]`
- `evidence_briefs.knowledge_base_ids text[]`，如果已有对应表则补字段。

所有 derived artifact 都应保留 source_refs，KB ids 只是索引和产品过滤，不替代证据。

## 6. API 计划

### Knowledge Base CRUD

新增：

```http
GET    /workspace/knowledge-bases
POST   /workspace/knowledge-bases
GET    /workspace/knowledge-bases/{knowledge_base_id}
PATCH  /workspace/knowledge-bases/{knowledge_base_id}
DELETE /workspace/knowledge-bases/{knowledge_base_id}
POST   /workspace/knowledge-bases/{knowledge_base_id}/restore
POST   /workspace/knowledge-bases/{knowledge_base_id}/pin
DELETE /workspace/knowledge-bases/{knowledge_base_id}/pin
```

列表响应字段：

```json
{
  "knowledge_bases": [
    {
      "knowledge_base_id": "kb_...",
      "name": "产品调研",
      "description": "",
      "kb_type": "document",
      "visibility": "private",
      "permission": "owner",
      "is_default": true,
      "is_pinned": false,
      "counts": {
        "source_items": 12,
        "documents": 12,
        "chunks": 148
      },
      "readiness": {
        "status": "ready",
        "embedding_coverage": 0.98,
        "processing_count": 0,
        "failed_processing_count": 0
      },
      "updated_at": "..."
    }
  ]
}
```

### Source/Document APIs 加 KB 参数

现有：

- `/workspace/sources/preview`
- `/workspace/sources`
- `/workspace/sources/sync`
- `/workspace/sources/upload`
- `/workspace/sources/text`
- `/workspace/documents/data`
- `/workspace/documents/delete`

增加兼容字段：

```json
{
  "knowledge_base_id": "kb_...",
  "knowledge_base_ids": ["kb_..."]
}
```

规则：

- 创建来源时没有传 KB：放入 default KB。
- 创建来源时传一个 KB：source config 和 ingest output 绑定该 KB。
- 创建来源时传多个 KB：MVP 不支持，返回 400；后续可做多 membership。
- 文档列表可按 KB 过滤。
- 文档删除默认只删除当前 KB membership；如果 source_item 没有任何 active membership，再进入普通 delete 流程。hard delete 仍需显式参数。

### Ask API 加 KB scope

现有 `scope.source_item_ids` 保留，新增：

```json
{
  "query": "这几份资料有什么共同结论？",
  "intent": "auto",
  "scope": {
    "mode": "hard",
    "knowledge_base_ids": ["kb_a", "kb_b"],
    "source_item_ids": [],
    "allow_expand_scope": false
  }
}
```

scope 解析规则：

- `knowledge_base_ids` 为空且无 source scope：默认当前 KB，若没有当前 KB 则 default KB 或 all visible，具体由 surface 决定。
- `knowledge_base_ids` 与 `source_item_ids` 同时存在：取交集，避免用户以为限定了 KB 但 source_item 跨库泄露。
- `doc_only`、附件、显式 source_item_ids 默认 hard。
- 多 KB soft mode 允许 graph/profile/memory，但 graph/memory source_refs 必须落在 `KB membership ∩ ACL-visible` 内，或在 response 中标记为 outside_scope 且不作为 citation。

响应新增：

```json
{
  "route": {
    "scope_applied": {
      "mode": "hard",
      "knowledge_base_ids": ["kb_a"],
      "knowledge_base_count": 1,
      "source_item_count": 18,
      "dropped_knowledge_base_ids": [],
      "dropped_source_item_ids": []
    }
  },
  "evidence": {
    "citations": [
      {
        "source_item_id": "src_...",
        "knowledge_base_id": "kb_a",
        "knowledge_base_name": "产品调研"
      }
    ]
  }
}
```

### Knowledge Base Search

新增调试/产品共用 endpoint：

```http
POST /workspace/knowledge-bases/search
```

Payload：

```json
{
  "query": "agent runtime",
  "knowledge_base_ids": ["kb_a"],
  "top_k": 8,
  "mode": "hybrid"
}
```

这可以替代 legacy `/workspace/search/query` 的产品使用，并明确多 KB contract。

### Move/Copy

```http
GET  /workspace/knowledge-bases/{source_kb_id}/move-targets
POST /workspace/documents/move
POST /workspace/documents/copy
```

Move payload：

```json
{
  "source_item_ids": ["src_..."],
  "source_knowledge_base_id": "kb_a",
  "target_knowledge_base_id": "kb_b",
  "mode": "reuse_vectors"
}
```

`mode`：

- `reuse_vectors`：embedding model、chunking config、parser config 兼容时允许。
- `reparse`：目标 KB 配置不同，重新生成 documents/chunks/embeddings。

## 7. 检索与 RAG 质量计划

### Scope resolution service

新增服务 `KnowledgeBaseScopeService`，职责：

```text
payload scope
  -> validate KB access
  -> resolve KB membership source_item_ids
  -> intersect ACL visible source_item_ids
  -> detect dropped/unauthorized/stale sources
  -> return ResolvedScope
```

`RetrievalService` 可以暂时继续只接 `source_item_ids`，由 API 层先解析 KB scope。第二阶段再把 KB-aware filtering 下沉到 store/vector search。

### Vector readiness

为了让 PSKA “至少作为 RAG 是 capable”升级成“默认好用”，需要：

- 产品入口创建/同步时使用 runtime embedding config，而不是固定 disabled provider。
- 允许知识库级 embedding config，但初期可全局统一。
- 新增 backfill job：按 KB 维度补 missing embeddings。
- readiness 显示 embedding coverage。
- Ask diagnostics 显示 vector 是否启用、candidate 数、fallback 原因。

### 多 KB embedding 规则

借鉴 WeKnora：

- 多 KB vector 检索前校验 embedding provider/model 一致。
- 一致时 query embedding 只算一次。
- 不一致时策略二选一：
  - MVP：返回可解释错误，提示用户分开提问或改用全部 lexical。
  - 后续：按 model 分组检索，再做归一化融合，但 UI 必须说明跨模型融合置信度较低。

推荐 MVP 使用“同模型才 hybrid，不同模型走 lexical fallback 并标记 degraded”，这样对用户更温和。

### Evidence check 继续作为答案闸门

现有 `_ask_verify_evidence` 应扩展：

- 检查 citation 是否属于 selected KB。
- dropped citation 标记 `reason=outside_knowledge_base_scope`。
- no-answer reasons 增加：
  - `selected_knowledge_bases_empty`
  - `selected_knowledge_bases_not_ready`
  - `embedding_missing_or_stale`
  - `scope_filtered_all_candidates`

### Evaluation

新增 domain-agnostic eval：

- 两个 KB 各有不同主题，查询 A 只能命中 KB A。
- 多 KB 查询能同时返回两个 KB 的 citations。
- 同一 query 在 all scope 与 current KB scope 有不同但可解释结果。
- hard scope 不泄露 graph/profile/memory citations。
- embedding disabled 时 lexical fallback 可工作。
- embedding enabled 时 vector candidates 出现在 score_debug。
- no-answer 不编造，并解释 KB scope/processing/permission。

## 8. 前端产品计划

### 与现有前端产品设计融合

当前 PSKA 前端是一个 Vite/React 单页工作台，主入口集中在 `frontend/src/App.tsx`：

```text
Today -> 写作 -> Graph -> 资料库 -> Review
```

现有产品心智不是“先进入知识库产品，再使用其他能力”，而是“资料库提供证据，Today/Ask、Digest、Review、Graph、Writing 都围绕证据工作”。因此多知识库前端不应空降一套独立的 WeKnora 式平台页，也不应新增一个和 `资料库` 并列的顶层 nav。推荐融合方式是：

```text
全局 KB scope state
  -> 资料库工作台显示和管理当前 KB
  -> Today/Ask 读取当前或多选 KB scope
  -> Graph/Review/Writing/Digest 显示 KB lineage 和过滤器
```

#### 现有页面的改造落点

1. Left Sidebar

现有 `LeftSidebar` 在 `mode === "corpus"` 时只显示一段说明。多 KB 后把这里升级为轻量知识库树：

- 顶部显示 `资料库` 和新建按钮。
- 列出最近/置顶 KB，点击后仍停留在 `corpus` mode，只切换 `currentKnowledgeBaseId`。
- 折叠侧栏时保持现有 icon-only 行为，不增加复杂导航。
- Today mode 下仍优先显示 Ask conversations，不把 KB 列表抢占 Today 的对话树。

2. TopBar

现有 `TopBar` 已承担 mode switch 和身份上下文。不要在这里塞完整 KB 管理，只放一个紧凑 scope chip：

- 在 `Today`、`资料库`、`Writing`、`Graph` 中显示当前 scope：`全部资料库` / `产品调研` / `3 个资料库` / `附件`。
- 点击打开 `KnowledgeBaseScopePicker`。
- 身份字段仍保留在右侧，避免和 tenant/user/represented_user_id 混淆。

3. CorpusWorkspace

`CorpusWorkspace` 是第一优先级改造点。它已经包含上传、粘贴、URL/RSS、高级同步、资料删除、chunk preview、Digest log、Evidence Brief 入口。多 KB 不应拆掉这些能力，而是给这些操作加当前 KB 语境：

- header 从“资料库”升级为“资料库 / 当前 KB”。
- `corpus-summary` 显示当前 KB counts，同时保留“全部资料”切换。
- `SourceIngestPanel` 上传/粘贴默认写入当前 KB。
- `SourceAdapterPanel` 添加 URL/RSS 默认绑定当前 KB。
- `DocumentLifecyclePanel` 默认只列当前 KB 文档。
- 删除按钮文案区分“从当前资料库移除”和“彻底清除资料”。
- `DigestLogPanel` 默认显示当前 KB 的 Digest，允许切换到全部。
- `ChunkPreviewPanel` 后续读取当前 KB 的 chunking config 作为默认值。

4. Today Ask composer

现有 Today composer 已有附件、Ask 参数、强制 deep。多 KB scope 应嵌入 `today-chat-tools` 左侧工具区：

- 附件按钮旁增加 scope picker。
- 默认使用 store 里的 `selectedKnowledgeBaseIds`。
- 发送时通过 `askWorkspaceStream(..., { scope })` 透传；这个 API 客户端已经支持通用 `scope`。
- 上传附件时，如果用户选择“加入资料库”，应进入当前 KB；如果只是临时附件，继续使用 attachment hard scope。
- AskResult 显示 `route.scope_applied`，并在 citation/source window 上展示 KB 名称。

5. AskConversationPanel / AskResult

现有 AskResult 已展示 route、progress、evidence、quality signals、no-answer diagnostics。多 KB 只需要补充：

- route label 增加 KB scope 摘要。
- citation 卡片显示 `knowledge_base_name`。
- dropped citation audit 显示 `outside_knowledge_base_scope`。
- no-answer diagnostics 显示“选中资料库无证据/未处理/权限过滤”等原因。

6. GraphWorkspace

Graph 当前是全局图谱视图。多 KB 后先加过滤器，不要复制一套 Graph 页面：

- 默认读取当前 KB scope。
- `loadGraphData`、`loadGraphSearchSubgraph`、Graph Ask 都带 `knowledge_base_ids`。
- Inspector 中 source_refs 展示 KB badge。
- 当 graph node 来自 scope 外证据时，只在 soft/all scope 出现；hard KB scope 不展示外部 citation。

7. WritingWorkspace

Writing board 是独立项目画布，不应被 KB 页面吞掉。融合方式：

- 创建 board 时可选择默认 KB scope。
- board card 显示绑定 KB。
- `buildWritingAskScope` 在 context_nodes/source_item_ids 之外追加 `knowledge_base_ids`。
- Evidence Brief 生成 Writing draft 时保留 KB lineage。
- compose 仍然 retrieval-free，只使用节点已有 citations/source_refs。

8. ReviewCenter

Review 是治理入口，不应拆成每个 KB 一个 Review 页面。先加过滤和 badge：

- Review item 卡片显示来源 KB。
- 支持按 KB 过滤。
- 当前 KB 的 Digest/Review 在 CorpusWorkspace 里展示摘要，完整处理仍跳到 ReviewCenter。

#### 前端状态与 API 客户端

新增类型位置：

- `frontend/src/types.ts`
  - `KnowledgeBase`
  - `KnowledgeBaseListResponse`
  - `KnowledgeBaseScope`
  - `KnowledgeBaseReadiness`

新增 API client：

- `listKnowledgeBases`
- `createKnowledgeBase`
- `patchKnowledgeBase`
- `deleteKnowledgeBase`
- `loadKnowledgeBase`
- `loadKnowledgeBaseDocuments`
- `moveWorkspaceDocuments`
- `copyWorkspaceDocuments`

扩展现有 API client：

- `loadCorpusData(serviceToken, limit, { knowledgeBaseIds })`
- `loadWorkspaceDocuments(serviceToken, includeDeleted, { knowledgeBaseId })`
- `createTextSource(..., { knowledge_base_id })`
- `uploadWorkspaceSource(..., { knowledge_base_id })`
- `createKnowledgeSource(..., { knowledge_base_id })`
- `syncKnowledgeSources(..., { knowledge_base_id })`
- `runDigestNow(..., { knowledge_base_ids })`
- `askWorkspaceStream(..., { scope: { knowledge_base_ids } })`

Zustand store 增加：

```ts
currentKnowledgeBaseId: string;
selectedKnowledgeBaseIds: string[];
knowledgeBaseScopeMode: "current" | "all" | "selected" | "attachments";
setCurrentKnowledgeBaseId(id: string): void;
setSelectedKnowledgeBaseIds(ids: string[]): void;
setKnowledgeBaseScopeMode(mode): void;
```

持久化建议：

- 使用 `sessionStorage`，key 带 tenant/user，例如 `pska_selected_kbs:${tenantId}:${representedUserId}`。
- 切换 tenant/user 后清空或重新读取，避免跨用户 scope 污染。
- 如果当前 KB 被删除，fallback 到 default KB。

#### 组件拆分建议

现有 `App.tsx` 已经很大，多 KB 不应继续堆所有逻辑。建议以低风险方式抽出组件：

- `KnowledgeBaseSwitcher`
- `KnowledgeBaseScopePicker`
- `KnowledgeBaseRail`
- `KnowledgeBaseStatusStrip`
- `KnowledgeBaseBadges`
- `ScopedCorpusHeader`

第一阶段可以仍放在 `App.tsx` 中开发，稳定后再移到 `frontend/src/components/knowledge-bases/`。不要先做大规模重构，以免影响 Today/Ask/Writing 的现有产品流。

#### 视觉融合原则

- 继续使用现有 operational dashboard 风格：紧凑、信息密度高、少营销化。
- 不做新的 landing page，不做 hero。
- KB list 可以用现有 card/list 样式，但它是资料库内部工作面，不是全站首页。
- Scope selector 使用轻量 popover/dropdown，不要占据主区。
- 统计条沿用 `corpus-summary` / `operation-stats`。
- 状态提示沿用 `corpus-operation` / `review-empty` / `pill`。
- 资料条目沿用 `document-lifecycle-card`，只增加 KB badge 和 move/copy 动作。

#### 前端渐进落地顺序

UI Phase A：scope 基础设施

- types/API/store。
- TopBar scope chip。
- Today Ask 透传 `knowledge_base_ids`。
- AskResult 展示 scope_applied。

UI Phase B：CorpusWorkspace KB 化

- 左侧 KB rail。
- 当前 KB header/counts/readiness。
- 上传/粘贴/URL/RSS 进入当前 KB。
- 文档列表按当前 KB 过滤。

UI Phase C：Digest/Review/Graph/Writing 联动

- Digest by KB。
- Review badge/filter。
- Graph KB filter。
- Writing board 默认 KB scope。

UI Phase D：高级资料管理

- move/copy/reparse。
- KB settings。
- shared/read-only。
- per-KB parser/chunking/embedding config。

这个融合路线的关键是：先让“当前知识库/选中知识库”成为现有工作台的上下文，再逐步把资料管理和派生知识按 KB 分组，而不是先重建一套并行前端。

### MVP 页面

1. 知识库列表页

- 创建知识库。
- 默认知识库 badge。
- 资料数、chunk 数、ready 状态。
- 进入详情。

2. 知识库详情页

- 资料列表。
- 上传/粘贴文本/添加 URL/RSS/folder。
- 同步按钮。
- processing spans。
- Ask 当前知识库。

3. Ask scope selector

- 当前 KB。
- 全部可见 KB。
- 多选 KB。
- 附件/指定资料。

### 第二阶段页面

- Move/copy documents。
- KB 设置：chunking/parser/embedding。
- Digest/Review tab。
- Evidence Brief / Writing 入口。
- Shared/read-only grouping。

### 交互细节

- 用户在 KB 详情页 Ask，默认 scope 是当前 KB hard/soft 由 intent 决定；普通 kb_search 可以 soft，但 citations 必须在 KB 内。
- 用户在全局 Ask，默认 scope 是“上次选择的 KBs”或“全部可见 KB”，需要在输入框上明确显示。
- 选择了未 ready 的 KB，Ask 按钮不禁用，但运行后 diagnostics 要清楚；如果完全无 chunks，再提示先处理资料。
- 资料删除要区分“从当前知识库移除”和“彻底删除资料”。

## 9. 与现有 PSKA 能力的衔接

### Source lifecycle

当前 `KnowledgeSourceService` 继续负责 folder/rss/url/text/upload-like source lifecycle。新增 KB 后：

- `add_folder_source` 等方法可接受 `knowledge_base_id`。
- `record_sync_report` 写入 sync_runs 后，根据 report.source_item_ids 更新 `knowledge_base_source_items`。
- `processing_spans` 增加 `knowledge_base_id` 可选字段，或者通过 source/membership 反查。

### RetrievalService

短期不大改 `RetrievalService`：

- API 层解析 KB -> source_item_ids。
- 传入 `source_item_ids` 和 `scope_mode`。
- response enrichment 时补 `knowledge_base_id/name`。

中期优化：

- store 增加 `list_chunks_for_knowledge_bases`。
- Postgres vector search 直接按 KB join/mapping 过滤。
- score_debug 增加 `knowledge_base_scope`。

### MCP / FastReAct

PSKA read-only MCP tools 需要新增参数：

- `knowledge_base_ids`
- `scope_mode`

FastReAct deep Ask 应把用户选择的 KB scope 传到 PSKA MCP tools，不能让 deep route 忽略前端 scope。

仓库边界：PSKA 侧 MCP/API scope 支持在 `/Users/xudawei/Documents/personal archive`；FastReAct 侧 deep Ask / Research Task 协议适配在 `~/FastReAct`。当前实现已提前同步 FastReAct `tool_policy.scope` 注入：运行时会在执行 PSKA MCP tool call 前写入 `knowledge_base_ids`、`source_item_ids` 和 `scope_mode=hard`，并在 tool-call metadata 中记录 `tool_policy_scope_applied=true`。

### Digest / Review

Digest job payload 增加：

```json
{
  "knowledge_base_ids": ["kb_..."],
  "source_item_ids": ["src_..."]
}
```

服务端解析后以 source_item_ids 执行，但 notes/claims/review_items 写回时保留 `knowledge_base_ids`。

### Writing Workspace

Writing board 可绑定 KB scope：

- 创建 board 时选择 KB。
- question node 调 Ask 时带 board 默认 KB。
- compose 不检索，继续只用原 answer nodes/source_refs。

## 10. 分阶段路线图

### Phase 0：语义收口和验收口径，1-2 天

产出：

- 明确 UI 文案：`资料库` 表示 corpus/document library，`知识` 表示 digest/review/graph/memory 沉淀；内部表可叫 `knowledge_bases`。
- API docs 增加 KB scope contract。
- 测试计划写入 docs。

验收：

- 旧 Ask/upload/sync/search 行为不变。
- 文档明确 `KnowledgeSource != KnowledgeBase`。

### Phase 1：数据库与 store 能力，2-4 天

产出：

- migration `021_knowledge_bases.sql`。
- dataclass/model/store protocol/in-memory store/Postgres store 支持 CRUD。
- backfill default KB。
- membership 写入、查询、删除/恢复。

验收：

- 现有数据迁移后每个用户有 default KB。
- 旧 source_items 都属于 default KB。
- 同一用户可创建多个 KB。
- 不同 KB membership 不互相污染。

### Phase 2：API 与 scope resolution，3-5 天

产出：

- KB CRUD endpoints。
- source/text/upload/sync 支持 `knowledge_base_id`。
- documents list/delete 支持 KB filter。
- `KnowledgeBaseScopeService`。
- Ask 支持 `scope.knowledge_base_ids`。
- MCP tools 支持 KB scope。

验收：

- Ask 当前 KB 不返回其他 KB citation。
- 多 KB Ask 返回多个 KB citation。
- unauthorized KB id 不泄露 metadata。
- deep Ask 通过 MCP 仍保留 KB scope。

### Phase 3：RAG readiness 和 embedding 默认化，3-5 天

产出：

- ingest/sync 使用 runtime embedding provider。
- KB 级 embedding coverage。
- backfill missing embeddings by KB。
- readiness API 增加 KB 维度。
- no-answer diagnostics 扩展。

验收：

- 新上传资料默认进入 hybrid retrieval。
- embedding disabled 时仍能 lexical RAG。
- embedding error 不阻塞 Ask，但 trace 标记 degraded。

### Phase 4：前端 MVP，4-7 天

产出：

- KB 列表页。
- KB 详情资料 tab。
- 当前 KB Ask。
- Ask scope selector。
- processing/readiness 状态。

验收：

- 用户能创建两个 KB，分别上传资料，分别提问。
- 用户能在全局 Ask 选择多个 KB。
- UI 清楚显示当前查询范围。
- 删除/恢复不会误删另一个 KB 的资料。

### Phase 5：Digest/Review/Writing KB 化，4-7 天

产出：

- digest by KB。
- review items 按 KB 过滤。
- Evidence Brief 保留 KB lineage。
- Writing board 默认 KB scope。

验收：

- 某 KB 的 digest 只使用该 KB 证据。
- Evidence Brief 的 citations/source_refs/KB lineage 完整。
- Writing follow-up Ask 使用 board KB scope。

### Phase 6：协作与高级操作，后续

产出：

- KB share/read-only/editor。
- move/copy/reparse。
- per-KB chunking/parser/embedding config。
- 多 KB cross-model retrieval 策略。
- KB copy/clear/export/import。

验收：

- 共享 KB 可读不可写。
- move/copy 有清晰进度和失败恢复。
- per-KB config 改动不会破坏已有 chunks，必要时要求 reparse。

### 当前落地状态

当前 `tenant` 分支已经覆盖 Phase 1、Phase 2 的 PSKA 侧主路径，并提前实现了部分 Phase 4/5 的 lineage 传递：

- `021_knowledge_bases.sql` 已新增 KB 与 membership 表，并通过临时 Postgres 迁移测试验证 legacy default KB backfill 和幂等性。
- in-memory store / Postgres store 已支持 KB CRUD、default KB、source/source_item membership、pin/archive/restore、membership 删除/加入/移动。
- workspace API 已支持 KB CRUD、source/text/upload/sync 绑定 `knowledge_base_id`、documents data/delete/link/move、KB search。
- Ask、KB search、MCP read tools、Digest、Review、Graph、Writing 已能接收或保留 `knowledge_base_ids` scope/lineage；hard scope 仍以 `knowledge_base_ids -> source_item_ids -> RetrievalService` 的安全路径执行。
- 前端已有 KB rail、scope chip、KB 管理、归档恢复、资料卡 KB badge、KB-scoped upload/text/source、documents membership 管理，以及 Ask/Digest/Review/Graph/Writing 的 scope 展示和传递。
- `frontend/e2e/multi-kb-scoped-ask.spec.ts` 已用真实 AuthNode/Gateway browser session 验证单 KB hard Ask 不泄露未选 KB 证据。
- 真实 FastReAct/PSKA MCP/DeepSeek smoke 已验证 deep Ask scope 透传：`scripts/pska-fastreact-kb-scope-smoke` 会复跑真实 alpha/beta KB 验收；FastReAct `pska_pska_search` / `pska_pska_read_evidence_context` / `pska_pska_graph_context` tool call 均带选中 KB/source 的 hard scope，未选 KB secret/ID 未进入答案或 trace，临时数据残留计数为 0。

仍需在 Phase 1 验收包中明确的边界：

- `docs/API_REFERENCE.md` 已补 `scope_applied`、`knowledge_base_ids`、`dropped_scope_ids`、citation KB attribution、documents membership 和 MCP KB scope 的正式 contract。
- KB readiness 目前能展示最小 counts/coverage/processing 状态；per-KB embedding/chunking 配置和 backfill by KB 仍属于后续阶段。

## 11. 测试与验收清单

### 单元测试

- `KnowledgeBaseScopeService`：
  - default KB fallback。
  - selected KB -> source_item_ids。
  - KB/source_item intersection。
  - unauthorized filtering。
  - empty KB diagnostics。

- Store：
  - create/list/update/delete KB。
  - source/source_item membership。
  - backfill idempotency。
  - tenant isolation。

- Retrieval：
  - hard KB scope。
  - soft KB scope with no citation leakage。
  - multi-KB result attribution。
  - embedding disabled fallback。

### 产品流测试

- 创建 KB A/B，分别上传资料，Ask A 不命中 B。
- 全局多 KB Ask 同时命中 A/B。
- 删除 KB A membership 后，A 不可检索，B 不受影响。
- hard delete source_item 后，相关 digest/review/graph support stale 或删除策略正确。
- doc_only 附件优先级仍然高于 KB scope。

### 集成 E2E

按项目约束，PSKA 集成验证必须通过：

```bash
./start.sh
```

然后扩展或新增：

```bash
./scripts/pska-weknora-coverage-e2e --config ".pska/config.json"
```

新增 coverage 项：

- `knowledge_base_crud`
- `knowledge_base_scoped_ingest`
- `ask_single_kb_scope`
- `ask_multi_kb_scope`
- `ask_scope_no_leak`
- `kb_readiness`
- `digest_review_kb_lineage`
- `writing_kb_scope`

## 12. 数据迁移策略

### Backfill 规则

对每个 `(tenant_id, owner_user_id)`：

1. 创建 default KB：
   - slug: `default`
   - name: `默认资料库`
   - is_default: true
   - default_space_id: 当前 private space。

2. 将该用户现有 active `knowledge_sources` 加入 `knowledge_base_sources`。

3. 将该用户现有 active `source_items` 加入 `knowledge_base_source_items`。

4. 对 deleted/inactive source_items：
   - 不默认加入 active membership。
   - 可写入 archived membership，或迁移时忽略，保留现有 deletion 状态。

### 回滚策略

第一阶段不删除旧字段，不改变 retrieval 主路径。若 KB 功能关闭：

- 旧 API 继续走 all visible source_items。
- 新 KB 表可以保留但不参与 scope。
- migration 不应破坏旧唯一约束和 source lifecycle。

### 幂等性

Backfill 必须可重复运行：

- KB 以 `(tenant_id, owner_user_id, slug)` upsert。
- membership `ON CONFLICT DO NOTHING` 或更新 status。
- 不覆盖用户手动改名，除非 KB 是系统创建且未被编辑。

## 13. 风险与决策点

### 风险 1：`space_id` 与 KB 概念混淆

不要把 space 当 KB。space 是权限/协作容器，KB 是 corpus/检索容器。一个 KB 可以有 default_space_id，但不能等同于 space。

### 风险 2：同一资料多 KB membership 的删除语义

如果同一 source_item 属于多个 KB，“从 KB 删除”不能等于删除 source_item。MVP 可先限制同一 source_item 单 active KB，或者实现 membership delete 和 hard delete 两级操作。

### 风险 3：跨 embedding model 多 KB检索

跨模型向量分数不可直接比较。MVP 需要明确 degraded/fallback，不要静默融合。

### 风险 4：deep Ask 忽略 KB scope

FastReAct deep 通过 MCP tools 检索，如果 MCP 不支持 `knowledge_base_ids`，会出现 quick scoped、deep unscoped 的产品不一致。当前实现已经通过 `tool_policy.scope` 运行时注入和真实 MCP smoke 降低该风险；后续风险主要转为回归风险，需要保留合同测试和真实 smoke 脚本/手册。

### 风险 5：Digest/Graph/Memory scope 外泄

Graph/Memory 是 PSKA 差异化，但在 KB hard scope 下必须严格限制 citation。现有 hard scope 会关闭 graph/profile/memory，这是安全起点；后续如果恢复 scoped graph，必须保证 source_refs 属于 selected KB。

## 14. 非目标

首轮不追求：

- 企业连接器矩阵。
- 公开知识库发布/embed。
- 完整 RBAC/RLS 后台。
- 多向量库 marketplace。
- 自动 Wiki 发布。
- 跨租户共享的完整组织权限系统。

这些能力可以预留字段，但不应阻塞“一账号多个知识库 + 稳定 RAG”上线。

## 15. 推荐优先级

最高优先级：

1. `knowledge_bases` + membership + default backfill。
2. Ask `scope.knowledge_base_ids`。
3. source/text/upload/sync 绑定 KB。
4. 单 KB/多 KB 不泄露检索测试。
5. KB list/detail + Ask scope selector。

第二优先级：

1. embedding 默认化和 backfill by KB。
2. KB readiness。
3. documents move/copy。
4. Digest/Review/Writing KB lineage。

第三优先级：

1. per-KB parser/chunking/embedding config。
2. shared/read-only 权限。
3. cross-model multi-KB retrieval。
4. KB copy/clear/export。

## 16. 最小可上线定义

可以认为“多知识库 MVP 完成”的条件：

- 一个账号可以创建、列出、重命名、删除多个知识库。
- 老数据自动进入默认知识库。
- 上传/粘贴/URL/RSS/folder source 可以进入指定知识库。
- 文档列表可以按知识库过滤。
- Ask 可以选择单个或多个知识库。
- hard scope 下不会返回未选 KB 的 citation/source_ref/source_window。
- no-answer 能解释选中 KB 没证据、没处理完成、或 embedding 不可用。
- 前端能清楚显示当前 Ask 查的是哪个知识库。
- 集成验证通过 `./start.sh` 启动的 PSKA，而不是分别启动前后端。

达到这个定义后，PSKA 就从“一个用户一堆资料”升级为“一个账号多个可检索知识库”，并为后续 Digest/Review/Writing 的产品差异化打好边界。
