# PSKA Architecture Status

日期：2026-06-18

成熟度等级：

- Prototype：概念或局部实现，真实使用风险高。
- MVP：主路径能跑通，有测试或 smoke gate，但体验和质量仍粗糙。
- MVP+：真实有限数据可用，有基本恢复、观测和质量门禁。
- Product-ready：可长期无人值守运行，有稳定操作、监控、回滚和用户体验。

## 模块成熟度矩阵

| 模块 | 目标职责 | 当前状态 | 成熟度 | 主要缺口 | 开源优先候选 | 下一步 |
| --- | --- | --- | --- | --- | --- | --- |
| Service daemon / HTTP API / MCP | 让 PSKA 作为在线服务被 CLI、HTTP client、FastReAct 和 MCP client 使用 | `/health`、`/ready`、search、agentic search、jobs、review、candidates、digest、HTTP MCP 已有第一版；`local-daemon` 是 foreground supervisor | MVP | 系统级 supervisor、日志轮转、status/pid、长期运行 polish | launchd、systemd、supervisord、honcho/foreman | 产品化 daemon 和运维入口 |
| Postgres schema / migrations | 保存 canonical source、chunks、embeddings、entities、hyperedges、review、jobs、audit | schema 和 migrations 覆盖 MVP 主路径 | MVP+ | schema 文档、数据质量报告、迁移兼容策略 | Alembic 风格迁移实践、pgvector、Postgres FTS | 补 schema map 和质量 gate |
| Source ingestion | 将 connector 输出规范化为 source/document/chunk | Twitter/X 和 files 主路径可用；connector record/state contract 已有第一版 | MVP+ | 更多 source 类型、失败恢复细节、导入质量报告 | Pydantic、JSON Schema | 保持 scope 收敛，先强化有限数据集 |
| Files connector | 授权目录扫描、文本抽取、manifest、watch | files-scan/files-sync/files-watch、PDF/DOCX optional extraction、manifest reconciliation 已有第一版 | MVP | OCR、复杂版面、更多格式、watch 长期运行质量 | watchdog、pypdf、python-docx、Unstructured、Docling、Marker | 先把 notes root 接入日常 workflow |
| Twitter connector | 导入 Twitter/X archive | zip import 和 channel payload 主路径可用 | MVP+ | 数据质量分析、链接展开、增量 archive 策略 | 标准 zip/json 解析；后续可评估 browser export 工具 | 作为 MVP 主要真实数据源继续打磨 |
| Chunks / embeddings / retrieval | ACL-first hybrid retrieval、citations、diagnostics | lexical/vector、optional rank-bm25、citations、graph context、diagnostics、PPR-style chunk/entity fusion、HippoRAG-style offline index、fact/entity embedding linking、offline index dirty/indexed/tombstone state 已有第一版 | MVP+ | rerank、Postgres FTS、query 分析质量、真实 replay 调参、持久化 adjacency/PPR graph cache | pgvector、Postgres FTS、rank-bm25、sentence-transformers/cross-encoder、Qdrant/LanceDB/Tantivy | 用 RAG-001 replay 持续调 PPR/RRF/rerank |
| Entities / hyperedges / GraphRAG | 构建可追溯关系图，辅助多跳检索 | Postgres hyperedge、entity alias linking、1-2 hop graph paths、evidence citations、HippoRAG-style offline fact/entity/passage index + online PPR + fact/entity embedding linking + offline index freshness 已有第一版 | MVP+ | 关系质量、冲突处理、图路径评测、长期图重要性、持久化 fact/entity/passage adjacency 和 embeddings cache | NetworkX、igraph、Neo4j/AGE 仅作后续评估、HippoRAG/PPR | 当前不是 GNN；是可退回普通 RAG 的 HippoRAG-inspired GraphRAG v0 |
| Review system | 让低置信/高影响候选由人类确认 | review taxonomy、enhanced review summary、relationship_candidate grounded apply 已完成第一版 | MVP+ | 批量操作、更多 apply 类型、review UI | Rich/Textual、diff libraries、JSON Patch | 扩展 review apply 和批处理 |
| Digest jobs | 空闲时做受预算约束的关联再消化 | digest job、scheduler、quota window、budget/explanation policy、FastReAct worker contract 已有第一版 | MVP+ | 真实 E2E 长期稳定性、实际 token 计量、关联触发策略质量评估 | FastReAct worker、LLM tracing、tiktoken/tokenizers | 稳定真实 digest write-back 和触发评估 |
| FastReAct integration | 把复杂 agentic loop 外包给 FastReAct | HTTP/MCP 边界、service token、jobs/run metadata、digest worker 已有第一版 | MVP | 真实长任务 trace、SSE 对齐、失败诊断、对话回流 | FastReAct 自身能力、OpenTelemetry 后续可评估 | 保存 PSKA 调用 FastReAct 的重要对话产物 |
| Agent conversation capture | 将 PSKA 调用 FastReAct 的重要问答/trace 存为资料 | reusable capture helper、conversation source material、`agentic-search --capture`、narrative briefing save 已完成第一版 | MVP | 敏感信息策略、去重、更多 agentic run 类型自动捕获 | JSONL tracing、OpenTelemetry、Pydantic models | 扩展 capture policy 和 retention/review |
| Human workflow / daily briefing | 让用户每天知道该看什么、做什么 | `daily-status`、`daily-briefing` deterministic v0、optional FastReAct narrative、review summary、memory/profile list 已完成第一版 | MVP+ | UI、批量 review、真实长期使用反馈、通知/inbox 体验 | Rich、Textual、Typer；后续可评估轻 Web UI | 下一轮根据真实使用反馈生成 backlog |
| Admin Console / Local Web Console | 给人类用户提供本地管理入口 | `/console`、review inbox、search/QA、memory/profile、jobs/ops、sources/connector 已有第一版；静态页面公开，数据 API 走 service token 和 PSKA HTTP 边界 | MVP | 用户工作台仍缺：对话主界面、语料/Wiki 浏览、富文本写作、选中文本建议、证据检查器 | 原生静态页面、HTMX/Alpine.js、Jinja2、Pico.css/Tailwind、Tiptap/ProseMirror 后续评估 | 不继续堆管理页，下一步转向 User Workspace |
| User Workspace / Chat & Writer | 让用户通过对话、资料浏览和写作使用 PSKA | 暂未形成独立产品面；目前 search 页面偏 API viewer，writer/editor 不存在 | Prototype | chat-first workflow、corpus explorer、chunk/entity/hyperedge 可读视图、selected-text 写作建议、evidence inspector、中文输出一致性 | Tiptap/ProseMirror、Monaco/CodeMirror、TanStack Table、HTMX/Alpine.js；参考 gbrain、llm_wiki、HippoRAG/PPR 评估 | 从 `APP-001` User Workspace Skeleton 开始 |
| Observability / config / operations | 发现服务、DB、FastReAct、jobs、connector 问题 | `/ready`、metrics、service-check、runbook、job ops 已有第一版 | MVP | worker-level health、真实 digest quality signal、日志长期管理 | structlog/loguru、OpenTelemetry、prometheus-client | 增加人类可读 ops briefing |

## 2026-06-18 基础设施体检

结论：PSKA 的当前技术基础设施仍然可用，可以进入下一轮产品迭代。

- Core test gate：`cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q` 通过，`222 passed in 13.74s`。
- Twitter/X channel gate：`cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q` 通过，`9 passed in 0.02s`。
- 受影响模块 gate：FastReAct integration、extraction、memory/hypergraph/agentic、retrieval eval 相关测试通过，`64 passed in 12.86s`。
- CLI contract：`python -m pska_core.cli --help` 可列出 db、serve、local-daemon、search、agentic-search、jobs、review、memory、profile、retrieval-eval 等入口。
- HTTP smoke：临时启动 `PSKA_SERVICE_TOKEN=smoke PSKA_FASTREACT_URL=http://127.0.0.1:9 python -m pska_core.cli serve --port 8877`，`/health` 返回 200，`/console` 返回 200，`/console/data` 未带 token 返回 401，带 `X-PSKA-Service-Token: smoke` 返回 200 并读取当前 `postgresql:///pska` 样例数据。

已知非阻塞问题：

- 当前样例库仍有若干 failed `digest_via_fastreact` jobs，主要来自 FastReAct timeout 或测试 canary；这说明 ops 页面有真实问题可展示，但不阻塞 PSKA 管理台、检索和下一轮 User Workspace 开发。
- GraphRAG 已从轻量 graph-assisted retrieval 前进到 HippoRAG-inspired GraphRAG v0：离线 fact/entity/passage 图索引对象、offline index dirty/indexed/tombstone state、fact/entity embedding linking、在线 query fact/entity/passage seeds、PPR score fusion 和 graph-connected chunk expansion 已有第一版。但它仍不是 GNN，也不是完整 HippoRAG 2 复现。

## GraphRAG / GNN 真实状态

PSKA 当前没有实现真正的 GNN，也没有达到完整 HippoRAG 2 级别的成熟 GraphRAG。2026-06-19 起，PSKA 已有一个 HippoRAG-inspired GraphRAG v0。

当前已经打下的基础是：

- Postgres-first entities 和 hyperedges。
- hyperedge source refs 和 evidence citations。
- ACL-filtered graph context。
- query seed entity 到 1-2 hop grounded graph paths。
- ACL-visible chunk/entity/hyperedge/evidence source ref 局部异构图。
- `HippoRAGOfflineIndex` 从 canonical chunk/entity/hyperedge/source_refs 构建 fact/entity/passage 图。
- `offline_index_states` 持久化 source/chunk 的 dirty/indexed/tombstone 状态、content hash、visibility version、embedding model、index version 和 last indexed time；当前在线 retrieval 仍可请求级重建 ACL-visible 子图作为 fallback。
- fact/entity embedding-style linking：fact embedding 使用实体+关系结构文本，entity linking 支持 query embedding，并带强信号/泛化门控。
- query-relevant facts、query-mentioned entities 和低权重 lexical/vector passage hits 作为 personalization seeds。
- lightweight personalized PageRank/random walk score fusion；graph expansion 的分数与 RRF 同量级，避免覆盖直接 lexical/vector 证据。
- graph-connected evidence chunks 可进入 retrieval results；无图信号时明确退回普通 RAG。
- 基于 mention、confidence、evidence coverage 和 path length 的轻量排序。
- retrieval diagnostics 可提示 insufficient/ungrounded evidence、graph conflict、sensitivity。

下一步仍不应直接上 GNN。更合理的顺序是：

1. 用 RAG-001 中文 replay 持续评估 expected citations、graph path 和 expanded chunk 命中。
2. 调整 PPR seeds、边权、damping、graph expansion 上限和 score fusion 权重。
3. 引入更成熟的 rerank 或 Postgres FTS。
4. 当真实问题稳定需要多文档多跳推理时，再评估更接近 HippoRAG 2 的 query-time 策略或图数据库/图算法库。

## Open-source-first 决策规则

新增或强化模块前，必须先回答：

- 是否存在成熟开源库能解决 80% 非核心问题？
- 该能力是否属于 PSKA 核心边界：ACL、source refs、review/audit、canonical data model、service contract？
- 如果不是核心边界，优先做薄封装而不是自研。
- 如果选择自研，必须在 TODO 中记录原因和替代方案。
