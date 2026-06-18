# PSKA Architecture Status

日期：2026-06-17

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
| Chunks / embeddings / retrieval | ACL-first hybrid retrieval、citations、diagnostics | lexical/vector、optional rank-bm25、citations、graph context、diagnostics 已有第一版 | MVP | fixture/eval、rerank、Postgres FTS、query 分析质量 | pgvector、Postgres FTS、rank-bm25、sentence-transformers/cross-encoder、Qdrant/LanceDB/Tantivy | 先做 retrieval eval，再决定是否引入外部 index |
| Entities / hyperedges / GraphRAG | 构建可追溯关系图，辅助多跳检索 | Postgres hyperedge、entity alias linking、1-2 hop graph paths、evidence citations 已有第一版 | MVP | 关系质量、冲突处理、图路径评测、PPR/HippoRAG 级多跳 | NetworkX、igraph、Neo4j/AGE 仅作后续评估、HippoRAG/PPR | 当前不是 GNN；先做 grounded GraphRAG eval |
| Review system | 让低置信/高影响候选由人类确认 | review taxonomy、enhanced review summary、relationship_candidate grounded apply 已完成第一版 | MVP+ | 批量操作、更多 apply 类型、review UI | Rich/Textual、diff libraries、JSON Patch | 扩展 review apply 和批处理 |
| Digest jobs | 空闲时做受预算约束的关联再消化 | digest job、scheduler、quota window、budget/explanation policy、FastReAct worker contract 已有第一版 | MVP+ | 真实 E2E 长期稳定性、实际 token 计量、关联触发策略质量评估 | FastReAct worker、LLM tracing、tiktoken/tokenizers | 稳定真实 digest write-back 和触发评估 |
| FastReAct integration | 把复杂 agentic loop 外包给 FastReAct | HTTP/MCP 边界、service token、jobs/run metadata、digest worker 已有第一版 | MVP | 真实长任务 trace、SSE 对齐、失败诊断、对话回流 | FastReAct 自身能力、OpenTelemetry 后续可评估 | 保存 PSKA 调用 FastReAct 的重要对话产物 |
| Agent conversation capture | 将 PSKA 调用 FastReAct 的重要问答/trace 存为资料 | reusable capture helper、conversation source material、`agentic-search --capture`、narrative briefing save 已完成第一版 | MVP | 敏感信息策略、去重、更多 agentic run 类型自动捕获 | JSONL tracing、OpenTelemetry、Pydantic models | 扩展 capture policy 和 retention/review |
| Human workflow / daily briefing | 让用户每天知道该看什么、做什么 | `daily-status`、`daily-briefing` deterministic v0、optional FastReAct narrative、review summary、memory/profile list 已完成第一版 | MVP+ | UI、批量 review、真实长期使用反馈、通知/inbox 体验 | Rich、Textual、Typer；后续可评估轻 Web UI | 下一轮根据真实使用反馈生成 backlog |
| Observability / config / operations | 发现服务、DB、FastReAct、jobs、connector 问题 | `/ready`、metrics、service-check、runbook、job ops 已有第一版 | MVP | worker-level health、真实 digest quality signal、日志长期管理 | structlog/loguru、OpenTelemetry、prometheus-client | 增加人类可读 ops briefing |

## GraphRAG / GNN 真实状态

PSKA 当前没有实现真正的 GNN，也没有达到 HippoRAG/PPR 级别的成熟 GraphRAG。

当前已经打下的基础是：

- Postgres-first entities 和 hyperedges。
- hyperedge source refs 和 evidence citations。
- ACL-filtered graph context。
- query seed entity 到 1-2 hop grounded graph paths。
- 基于 mention、confidence、evidence coverage 和 path length 的轻量排序。
- retrieval diagnostics 可提示 insufficient/ungrounded evidence、graph conflict、sensitivity。

下一步不应直接上 GNN。更合理的顺序是：

1. 建 retrieval/GraphRAG fixture 和 expected citations。
2. 评估 query 到 graph path 的相关性和 grounded 质量。
3. 引入更成熟的 rerank 或 Postgres FTS。
4. 当真实问题稳定需要多文档多跳推理时，再评估 HippoRAG/PPR 或图数据库/图算法库。

## Open-source-first 决策规则

新增或强化模块前，必须先回答：

- 是否存在成熟开源库能解决 80% 非核心问题？
- 该能力是否属于 PSKA 核心边界：ACL、source refs、review/audit、canonical data model、service contract？
- 如果不是核心边界，优先做薄封装而不是自研。
- 如果选择自研，必须在 TODO 中记录原因和替代方案。
