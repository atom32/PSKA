# PSKA / WeKnora 核心覆盖验收

本文是 tenant 线的竞品对照验收说明。目标不是复刻 WeKnora 的每个企业平台能力，而是确认 PSKA 已经覆盖“企业 RAG/Agent 平台的核心使用问题”，并明确展示 PSKA 的反打链路：

```text
Digest -> Candidates -> Review -> Discovery -> Graph/Memory -> Writing
```

## 验收口径

当用户问“我为什么不用 WeKnora 而用 PSKA”时，我们至少要能现场证明：

- 能接数据：folder、RSS/Atom、URL source 可 preview、create、sync。
- 能看处理过程：source sync report、processing spans、chunk preview 可解释资料有没有进入索引。
- 能问答：Ask PSKA 能返回答案、引用、source refs、progress；无答案时能解释原因。
- 能沉淀知识：digest 产出 digest notes、claims、review candidates、memory/relationship candidates。
- 能治理写入：低置信或高影响对象进入 Review，不直接写长期知识。
- 能服务写作：Evidence Brief 把 digest/review 证据变成带 citations/source_refs/lineage 的 Writing draft。
- 能做就绪检查：readiness 能看 database、schema、MCP、FastReAct、index、digest worker。

## 一键验收脚本

前置条件：

```bash
cd /Users/xudawei/Documents/AuthNode
./start.sh

cd /Users/xudawei/FastReAct
./start.sh

cd /Users/xudawei/Documents/personal\ archive
./start.sh
```

运行核心 coverage E2E：

```bash
./scripts/pska-weknora-coverage-e2e --config ".pska/config.json"
```

脚本会创建隔离 tenant/user，并写入一组虚构资料到：

```text
~/PSKA_workspaces/tenants/<tenant_id>/users/<user_id>/sources/weknora-coverage-<run_id>/
```

随后它会执行：

1. `GET /workspace/readiness`
2. `POST /workspace/chunking/preview`
3. `POST /workspace/sources/preview` for folder/RSS/URL
4. `POST /workspace/sources` for folder/RSS/URL
5. `POST /workspace/sources/sync`
6. `GET /console/sources/data`
7. `POST /workspace/ask`
8. `POST /workspace/ask/stream`
9. `POST /workspace/ask` 的 no-answer probe
10. `digest-now --skip-sync --source-item-id ...`
11. `GET /workspace/digest/data`
12. 必要时用 `/candidates` 写入 deterministic fallback candidates
13. `POST /workspace/evidence-briefs`

输出是 JSON evidence report，其中 `coverage` 字段是验收矩阵。所有条目 `ok=true` 才算通过。

## Strict 与稳定模式

默认模式优先验证 PSKA 产品链路，所以如果 FastReAct digest 没有写 candidates，脚本会用同一批 `source_refs` 通过 `/candidates` 写入一组 deterministic 测试候选，并在报告中标记：

```json
{
  "candidate_seeded": true,
  "candidate_seed_reasons": ["missing_digest_notes_or_claims", "missing_review_candidates"]
}
```

这不是伪装 FastReAct 成功，而是把两件事拆开：

- FastReAct 写回能力：看 `digest.command.diagnostics`、`candidate_write` 和 `candidate_seed_reasons`。
- PSKA 治理能力：看 Review、Evidence Brief、source_refs、lineage 是否能继续工作。

如果要严格验收 FastReAct 是否真实写入 candidates，使用：

```bash
./scripts/pska-weknora-coverage-e2e \
  --config ".pska/config.json" \
  --strict-fastreact-candidates
```

严格模式下，只要需要 deterministic fallback，就会失败。

## 覆盖矩阵

| WeKnora 核心问题 | PSKA 验收项 | 当前证明路径 |
| --- | --- | --- |
| 能不能接数据？ | folder、RSS/Atom、URL source preview/create/sync | `source_adapters` |
| 能不能看处理进度？ | sync report、processing spans、source cards | `processing_transparency` |
| 能不能调 chunk？ | 输入文本 + processing config 返回 chunks/stats/diagnostics | `chunk_preview` |
| 能不能问答？ | Ask quick 返回 answer、citations、source_refs | `ask_rag` |
| Agent/RAG 有没有进度？ | SSE `progress` 事件覆盖 understand/search/read/generate 等阶段 | `ask_progress` |
| 没答案怎么解释？ | no-answer diagnostics 包含 evidence、retrieval、permissions、FastReAct、MCP 维度 | `no_answer_diagnostics` |
| 能不能沉淀知识？ | digest notes、knowledge claims、review candidates | `digest_review_governance` |
| 能不能变 Wiki/Brief？ | Evidence Brief 生成 Writing board draft，保留 source_refs/lineage/review 状态 | `evidence_brief_writing` |
| 模型/Agent 是否 ready？ | `/workspace/readiness` 聚合 DB/schema/MCP/FastReAct/index/digest worker | `readiness` |

## Demo 讲法

WeKnora 更像企业 RAG/Agent 平台：接数据、问答、引用、Wiki 化。

PSKA 的讲法应该更具体：

1. 先接资料，不要求用户一次性信任系统。
2. 处理过程透明：source、sync、chunk、spans 都能看。
3. Ask 不只是回答；它必须解释证据、引用和找不到的原因。
4. Digest 把资料变成候选知识，而不是直接污染长期记忆。
5. Review 决定哪些 claim/memory/relationship 进入 Graph/Memory。
6. Evidence Brief 把可审阅证据推到 Writing，服务写作和长期理解。

也就是说：WeKnora 帮用户问资料；PSKA 还要帮用户长期消化资料。

## 明确不追平的能力

这些不是 6-8 周“核心可替代”范围，不应在当前验收里伪装完成：

- Feishu/Notion/Yuque 等企业连接器矩阵。
- 企业级 RBAC、审批流、审计后台和 Postgres RLS 全套上线形态。
- 网站 embed、IM 渠道、公开知识库发布。
- 多向量库矩阵和完整 MCP marketplace。
- 无审自动 Wiki 发布。

当前产品口径是：PSKA 做本地私有知识工作区，先覆盖核心 RAG/Agent/Wiki 使用问题，再用 Digest/Review/Writing 做差异化。

## 失败排查

- PSKA 未启动：先在本仓库运行 `./start.sh`，再跑 E2E。
- `readiness.summary.fastreact_ok=false`：检查 FastReAct 是否运行在 `http://127.0.0.1:8000`，以及 `.pska/config.json` 的 FastReAct token。
- `source_adapters=false`：检查 source preview/create/sync 返回值和 `GET /console/sources/data`。
- `ask_rag=false`：确认 source sync 后确实有 chunks；查看 `quality_signals.no_answer_diagnostics`。
- `digest_review_governance=false`：看 `digest.command.diagnostics`；默认模式会 fallback seed candidates，strict 模式会直接失败。
- `evidence_brief_writing=false`：确认 digest/review artifact 至少有一个带 `source_refs`。
