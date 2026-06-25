# PSKA Architecture Status

日期：2026-06-23

本文是当前 PSKA 系统功能点与成熟度的权威表，用来回答：系统现在有什么、成熟到什么程度、下一步缺什么。历史 MVP/TODO/roadmap 快照已经归档到 [`../../docs/archive/`](../../docs/archive/)，不再作为当前计划来源。

## 成熟度等级

- Prototype：概念或局部实现，真实使用风险高。
- MVP：主路径能跑通，有测试或 smoke gate，但体验和质量仍粗糙。
- MVP+：真实有限数据可用，有基本恢复、观测和质量门禁。
- Product-ready：可长期无人值守运行，有稳定操作、监控、回滚和用户体验。

## 模块成熟度矩阵

| 模块 | 目标职责 | 当前状态 | 成熟度 | 主要缺口 | 下一步 |
| --- | --- | --- | --- | --- | --- |
| Service daemon / HTTP API / MCP | 让 PSKA 作为本地在线服务被 CLI、HTTP client、FastReAct 和 MCP client 使用 | `/health`、`/ready`、workspace/console API、search、agentic search、jobs、review、candidates、digest、HTTP MCP 已有第一版；`local-daemon` 是 foreground supervisor；`./start.sh` 可启动后端和前端 | MVP | 系统级 supervisor、日志轮转、长期运行 polish、用户可读恢复入口 | 继续产品化 daemon/status/logs，把恢复命令收敛到日常入口 |
| Postgres schema / migrations | 保存 canonical source、documents、chunks、embeddings、entities、hyperedges、review、jobs、audit、Knowledge Sources | schema 和 migrations 覆盖当前主路径；Postgres 是 canonical DB；config 只作为启动/default seed | MVP+ | schema map、数据质量报告、迁移兼容策略 | 补 schema map 和数据质量 gate |
| Knowledge Sources / Source ingestion | 用用户可理解的 Source 模型管理授权来源和同步生命周期 | Knowledge Source 是用户主模型；connector state 降级为 adapter/runtime detail；folder sources、Twitter/X archive inbox、manual/channel payload 都落到 source/document/chunk | MVP+ | Add Folder/Remove Folder UI、source diagnostics、同步失败恢复细节、更多 source 类型 | 优先做 Knowledge Sources/file management UI 和 “why not found?” 诊断 |
| Files sync / local documents | 授权目录扫描、文本抽取、manifest、watch 和增量同步 | `files-sync`/`files-watch`/`files-scan` 可用；支持 text-like 文件、PDF/DOCX/XLSX 抽取、可选 legacy XLS 抽取、manifest reconciliation、new/changed/unchanged/moved/missing report；支持 opt-in `.pska-source.json` source collection，把一个目录作为一个 source、多个文件作为 documents；`digest-now` 前置 sync | MVP+ | OCR、复杂版面、更多格式、watch 长期质量、用户可见 sync report | 把 watched folders、last sync、sync report 和 unmonitored folder 提示做进 UI |
| Twitter/X archive ingestion | 导入 Twitter/X archive 作为高价值个人知识流 | workspace archive inbox 已接入 `files-sync` 和 `digest-now`；ZIP content hash 幂等；未变化跳过，内容变化会作为更新导入 | MVP+ | 链接展开、数据质量分析、批量导入反馈、archive inbox UI | 保持为 MVP 主要真实数据源，补导入质量报告 |
| Chunks / embeddings / retrieval | ACL-first hybrid retrieval、citations、diagnostics | lexical/vector/graph 检索主路径可用；支持 citations、graph context、offline index dirty/indexed/tombstone state、fact/entity linking、PPR-style fusion | MVP+ | rerank、Postgres FTS、真实 replay 调参、embedding coverage/quality UI | 用 replay 持续调 RRF/PPR/rerank，并补检索健康卡 |
| Entities / hyperedges / GraphRAG | 构建可追溯关系图，辅助多跳检索和 evidence review | Postgres hyperedges、entity alias linking、source refs、evidence citations、1-2 hop graph paths、HippoRAG-inspired offline/online retrieval 已有第一版 | MVP+ | 关系质量、冲突处理、图路径评测、长期图重要性 | 先做 replay/rerank/FTS 和图路径质量评估，不直接上 GNN |
| Discovery system | 把机器发现和长期知识写入隔离在 review 边界前 | `DiscoveryItem`、fingerprint、score/quality signals、Today/Discoveries feed 已有第一版；topic discovery 可见但质量仍依赖 corpus 和 producer | MVP | 高价值 producer、accept/ignore/snooze 生命周期持久化、和 review 的清晰桥接 | 先调 discovery quality，再扩 producer 数量 |
| Review system | 让低置信/高影响候选由人类确认后再进入 Memory/Profile/Graph | review taxonomy、approve/reject/apply、relationship_candidate grounded apply、fallback digest review 已有第一版；console/workspace 部分接入 | MVP+ | 批量操作、更多 apply 类型、完整 Review workspace、snooze/later 持久化 | 完成 Review workspace 和批量 review |
| Digest jobs | 空闲时对新资料做受预算约束的关联再消化 | `digest-schedule`、`digest-scheduler`、`digest-now`、quota、dedupe、explanation policy、FastReAct worker contract 已有第一版；scheduler 默认 300 秒增量检查，不是每天固定触发；no-candidate 会 diagnostics + fallback review | MVP+ | 真实长期稳定性、token/成本计量、关联触发策略质量、FastReAct trace 诊断 | 稳定 digest write-back，补质量指标和失败诊断 |
| FastReAct integration | 把复杂 agentic loop 外包给 FastReAct，PSKA 保持 DB/API/ACL 边界 | HTTP/MCP 边界、service token、job/run metadata、digest worker、agentic search、daily narrative 已有第一版 | MVP | 长任务 trace、SSE 对齐、tool failure 诊断、对话回流策略 | 保存重要 FastReAct 产物并加强失败可解释性 |
| Agent conversation capture | 将重要问答/trace 存为可追溯资料 | reusable capture helper、dedupe、retention/review policy、`agentic-search --capture`、daily narrative save 已有第一版 | MVP | 敏感信息策略、更多 run 类型自动捕获、长期保留策略 UI | 扩展 capture policy 和 review/retention 可见性 |
| Human workflow / daily briefing | 让用户每天知道系统状态、待处理事项和下一步动作 | `daily-status`、`daily-briefing` deterministic v0、optional narrative、review summary、memory/profile list、recommended commands 已有第一版 | MVP+ | 通知/inbox、真实长期反馈、与 Workspace 首页深度融合 | 把 daily workflow 合进 User Workspace 首页 |
| Admin Console / Local Web Console | 给开发/运维提供本地管理入口 | `/console`、review inbox、search/QA、memory/profile、jobs/ops、sources/runtime summary 已有第一版；数据 API 走 service token | MVP | 管理页体验粗糙、和 User Workspace 职责边界需继续收敛 | 不继续堆管理页，主产品入口转向 User Workspace |
| User Workspace / Chat & Writer | 让用户通过 Today、发现、资料浏览、搜索、图谱和写作工作流使用 PSKA | User Workspace scaffold 已存在；Today、Discoveries、Corpus、Brain/Search、Graph、review-adjacent surfaces 部分真实接入 | MVP | Knowledge Sources/file management、durable editor/canvas、chat-first workflow、证据检查器、中文输出一致性 | 先补 Knowledge Sources 和 corpus/review 主工作流，再做 durable editor/canvas |
| Observability / config / operations | 发现服务、DB、FastReAct、jobs、sources、sync、digest 问题 | `/ready`、metrics、`service-check`、`local-daemon status`、`mvp-status --summary`、sync report、job ops、fallback review 已有第一版 | MVP+ | worker-level health、digest quality signal、日志长期管理、面向用户的故障解释 | 做人类可读 ops briefing 和 source/search diagnostics |

## 当前检查方式

本文不依赖某个本地样例库的瞬时计数，也不写死某次 test run 的 passed 数。刷新或验收本表时优先使用这些检查：

```bash
git diff --check

cd core
../.pska/venvs/pska-py312/bin/python -m pytest tests -q

cd ..
./scripts/pska --config .pska/config.json mvp-status --summary
./scripts/pska --config .pska/config.json digest-now
```

Markdown 文档更新还应运行相对链接检查，确认所有本地链接仍存在。`digest-now` 的输出应关注：

- sync totals 是否包含 folder source 和 Twitter archive inbox。
- scheduled/worker/candidate_write 是否能解释本次 digest。
- FastReAct 未写 candidates 时，diagnostics 和 fallback review 是否可见。

## GraphRAG / GNN 真实状态

PSKA 当前不是 GNN，也不是完整 HippoRAG 2 复现。当前路线是 Postgres-first、ACL-first、可退回普通 RAG 的 HippoRAG-inspired GraphRAG v0。

已经具备的基础：

- Postgres entities、hyperedges、source refs 和 evidence citations。
- ACL-filtered graph context 和 1-2 hop grounded graph paths。
- `offline_index_states` 记录 source/chunk dirty/indexed/tombstone、content hash、visibility version、embedding model、index version 和 last indexed time。
- fact/entity embedding-style linking、query fact/entity/passage seeds、PPR-style score fusion。
- graph-connected evidence chunks 可进入 retrieval results；无图信号时退回普通 RAG。
- retrieval diagnostics 可提示 insufficient/ungrounded evidence、graph conflict、sensitivity。

下一步仍不应直接上 GNN。更合理的顺序是：

1. 用真实 replay 持续评估 expected citations、graph path 和 expanded chunk 命中。
2. 调整 PPR seeds、边权、damping、graph expansion 上限和 score fusion 权重。
3. 引入更成熟的 rerank 或 Postgres FTS。
4. 当真实问题稳定需要更强多文档多跳推理时，再评估更接近 HippoRAG 2 的 query-time 策略或图数据库/图算法库。

## 和其他文档的关系

- [`../../docs/README.zh.md`](../../docs/README.zh.md)：当前文档地图。
- [`../../docs/FEATURE_REALITY_CHECK.md`](../../docs/FEATURE_REALITY_CHECK.md)：前端/产品表面 Real、Partial、Mock、Planned 检查表。
- [`../../docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)：系统数据流、source model、discovery invariants 和 runtime processes。
- [`product-design-zh.md`](product-design-zh.md)：产品方向和用户工作流。
- [`../../docs/archive/`](../../docs/archive/)：历史 MVP/TODO/roadmap 状态，不再是当前计划来源。

## Open-source-first 决策规则

新增或强化模块前，必须先回答：

- 是否存在成熟开源库能解决 80% 非核心问题？
- 该能力是否属于 PSKA 核心边界：ACL、source refs、review/audit、canonical data model、service contract？
- 如果不是核心边界，优先做薄封装而不是自研。
- 如果选择自研，必须在当前计划或实现说明中记录原因和替代方案。
