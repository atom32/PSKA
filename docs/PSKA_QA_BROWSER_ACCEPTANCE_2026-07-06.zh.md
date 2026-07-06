# PSKA Quick / Deep QA 验收报告

日期：2026-07-06  
分支：`tenant`  
测试用户：`tenant_graphintell / test_user_3`  
测试入口：真实浏览器 `http://127.0.0.1:5173/` + 同一后端审计接口  
测试资料库：`海康威视年报`、`默认资料库`

## 结论

PSKA 的 Phase 1 基础问答链路仍然成立，但这轮验收发现两个关键问题：

1. `rank-bm25` 原来仍是 optional dependency，BM25 可能无声退回 term-frequency。已改为核心依赖并在本地 venv 安装。
2. 后端声称 `hybrid`，但 pgvector 在 hard scope 下实际返回 `vector_candidates=0`。原因是全局向量索引排序与 source scope 过滤组合后吞掉候选。已修复为 materialized scoped CTE，修复后 `vector_candidates=20`。

修复后，底层检索状态：

| 项目 | 结果 |
| --- | --- |
| Retrieval mode | `hybrid` |
| Lexical ranker | `rank_bm25` |
| Lexical candidates | 1711 |
| Vector enabled | true |
| Vector candidates | 20 |
| Vector error | null |
| Embedding model | `BAAI/bge-m3` |

## 当前路径说明

Quick 和 Deep 仍然是两条路径。

Quick：
`PSKA retrieval -> evidence scoring -> validation -> citation selection -> answer pipeline`，然后尝试让 FastReAct 做一次无工具总结。若总结不可用，会退回确定性证据摘要。

Deep：
FastReAct 负责 agentic loop，使用 PSKA read-only MCP 工具，例如 `pska_pska_search`、`pska_pska_read_evidence_context`、`pska_pska_graph_context`、`pska_pska_digest_context`。Deep 不应直接绕过 PSKA Evidence Pipeline。

BGE-M3：
当前本地 `bge-m3` 模型由 PSKA backend 进程加载为长生命周期 embedding provider。chunk embeddings 存 DB；query embedding 每次 search 计算。只要 backend 进程不退出，模型会常驻内存。

## 浏览器验收

### Quick：2025 营业收入

问题：只根据当前知识库回答 2025 年海康威视营业收入（元）是多少，并引用文件名。

结果：

- Scope：`海康威视年报` hard scope，5 sources，embedding 100%。
- 引用：`海康威视2025.pdf`。
- 正确数值 `92,507,796,069.94` 出现在证据和答案中。
- UI 曾显示 `Agentic 归纳不可用，已使用确定性证据摘要兜底`。
- 答案没有遵守“只回答数值”，而是拼接了多段证据摘要。

评价：检索和引用通过；答案抽取/最终表达不够满意。

### Deep：2024 vs 2025 营业收入比较

问题：比较海康威视 2024 年和 2025 年营业收入，给出两个年份数值、变化金额和变化百分比，并逐条引用。

结果：

- Scope：`海康威视年报` hard scope。
- 路径：`FastReAct MCP`。
- UI 总耗时约 113s。
- 最终回答：`[STOPPED] Task stopped due to maximum iteration limit (20)`。
- 引用 2 条，但落到 `2024 致股东/2025 目录` 等非答案段。

评价：Deep 工具链可用，但 agentic loop 没有稳定停在足够证据状态；当前不应判定通过。

## API 审计矩阵

### Hybrid Search

| Case | Scope | 结果 |
| --- | --- | --- |
| 2025 营业收入 | 海康威视年报 | Top results 均为 `海康威视2025.pdf`，hybrid 修复后 vector candidates = 20 |
| 2025 Q4 营业收入 | 海康威视年报 | Top results 包含分季度表，正确值在证据中 |
| 2024/2025 对比 | 海康威视年报 | 同时召回 2025 与 2024 证据，但答案组合仍需 Phase 2 Evidence Composition |
| 火星大气比例 | 海康威视年报 | Retrieval 会捞到无关财报块，最终 Quick no-answer 正确拒答 |
| 2025 营业收入 | 默认资料库 | 默认资料库也包含海康 2025 PDF，因此能答不是串库；这是资料重复/KB 管理问题 |

### Quick Ask

| Case | 结果 | 评价 |
| --- | --- | --- |
| 2025 营业收入 | supported，引用 6，答案含正确证据但不够收敛 | 部分通过 |
| 2025 Q4 营业收入 | supported，答案含 `26,750,013,612.93` | 部分通过 |
| 火星大气比例 | `no_answer`，0 citation，flags: `no_evidence`, `selected_knowledge_base_no_relevant_chunks` | 通过 |

## Fallback 清单

| Fallback | 当前状态 | 风险 |
| --- | --- | --- |
| BM25 缺失退回 term-frequency | `rank-bm25` 已改核心依赖；代码 fallback 仍存在 | 应在 ready/service-check 中暴露为 degraded |
| Vector search 异常时 lexical 继续 | 仍存在，`vector_error` 可审计 | 合理，但不能静默 |
| Vector candidates 为空但无 error | 本轮已修复 scoped pgvector 查询 | 已解决关键问题 |
| Quick agentic synthesis 不可用退回确定性摘要 | 浏览器观察到 | 影响答案可读性和指令遵循 |
| Deep FastReAct 不可用退回 Quick | 代码存在 | 合理，但 UI 必须显式标降级 |
| Deep agent loop 超迭代停止 | 浏览器观察到 `[STOPPED]` | 高风险，需 Answer Validation 拦截为失败而非 supported |
| Offline index `request_scoped_rebuild` | service-check 可见 | 可接受，但应纳入性能指标 |
| Graph ranker `rag_fallback` | 当前无 Graph PPR 时出现 | 可接受，Phase 2 再治理 |
| 文件解析 external parser fallback local parser | 代码存在 | 合理，需在 ingestion report 暴露 |
| 前端 stream -> non-stream / ask -> legacy search | 兼容 fallback | 只应保留为兼容，不应掩盖主链路失败 |

## 当前可用性判断

可以正常使用：

- 多 KB scope 选择与隔离。
- BM25 + BGE-M3 hybrid search。
- PDF 全文检索。
- 表格证据召回。
- No-answer policy。

仍未达到满意：

- Quick 的最终 Answer Extraction 太像证据拼接，未稳定遵守“只回答数值/字段”。
- Deep agentic loop 会过度迭代，且停止结果未被严肃判失败。
- 多证据计算题仍是 Phase 2 Evidence Composition，不应靠 retrieval rule 硬拧。

## 建议下一步

1. 把 Deep `[STOPPED]` 纳入 Answer Validator，不能标为 supported。
2. 给 Quick Answer Extraction 增加通用的 value/field-focused extractor，减少整段证据拼接。
3. 在 service-check 中增加 `rank_bm25_available`、`vector_candidates_smoke`、`embedding_coverage_by_kb`。
4. 把 `默认资料库` 中重复的海康 PDF 标记为 KB 管理问题，不当作 scope 泄漏。
