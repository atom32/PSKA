# PSKA Quick / Deep QA 浏览器验收报告

日期：2026-07-06  
分支：`tenant`  
测试用户：`tenant_graphintell / test_user_3`  
测试入口：真实浏览器 `http://127.0.0.1:5173/`
测试资料库：`海康威视年报`、`默认资料库`

## 结论

本轮复测后，PSKA 的 Quick 与 Deep 主链路均已可用：

- Quick 能在 hard KB scope 下完成 hybrid 检索、引用选择，并调用 FastReAct 做一次无工具最终归纳。
- Deep 能走 FastReAct agentic loop，通过 PSKA read-only MCP 工具完成多轮检索与证据读取。
- 本轮未出现 max-token、接收失败、MCP 未加载、`[STOPPED]` 或 prompt 被截断导致的错误。

仍需保留为风险：

- Deep 的最终答案数值正确，但 Citation/Evidence Set 后处理仍可能挂入不够相关的同文档/同年份 chunk。
- `/v1/runs` 列表接口在历史记录很大时会卡顿，审计应优先按 run id 查询或增加分页索引。
- BM25 缺失时的 term-frequency fallback 仍存在，虽然当前 `rank-bm25` 已是核心依赖且本地可导入；后续应在 service-check/readiness 中显式标记 degraded。

## 运行环境

| 项目 | 状态 |
| --- | --- |
| AuthNode 登录 | PSKA `/login` 读 config 后跳转 AuthNode `/login` |
| PSKA gateway | `http://127.0.0.1:5173/`，本地 `public_url` 来自 `.pska/config.json` |
| PSKA backend | `http://127.0.0.1:8765` |
| FastReAct | `http://127.0.0.1:18741` |
| Lexical ranker | `rank_bm25` 可导入 |
| Embedding | 本地 BGE-M3，KB embedding coverage 100% |
| Hybrid search | BM25 + embedding |

本地 `127.0.0.1` 是开发配置/示例值；登录回跳与 AuthNode 地址不在 gateway 逻辑里硬拼，而是由配置和请求头推导。

## 浏览器验收

### Quick：2025 营业收入

问题：`浏览器验收：海康威视2025年营业收入是多少？请用一句话回答，并引用文件名。`

结果：

- 路径：`资料库检索 · 快速回答 · 证据回答`
- Scope：`海康威视年报` hard scope，5 sources，范围可检索。
- PSKA run：`askrun_04bc02dd30ee4ab1814d266f5a7d34f1`
- FastReAct run：`45d68ead-9994-4082-9cc2-92ecf66b6e57`
- 答案：`海康威视2025年营业收入为92,507,796,069.94元（约925.08亿元），数据来源于《海康威视2025.pdf》。`
- 引用：`海康威视2025.pdf / chk_008493651fad531ea6b6db25f8ff99d4_78`，命中收入表。

PSKA/FastReAct 对账：

| 项目 | 结果 |
| --- | --- |
| PSKA route | `retrieval_owner=pska`, `selected_intent=quick` |
| Quick 最终归纳 | `selected_owner=fastreact_agentic_service` |
| FastReAct tool policy | `mode=none`, `visible_tools=[]` |
| FastReAct max iterations | `1` |
| FastReAct 入参长度 | 6408 字符 |
| 入参截断标记 | 无 |
| LLM usage | prompt 5403 / completion 135 / total 5538 |
| FastReAct error | null |
| content_truncated | final answer false |

评价：通过。Quick 不再只是证据拼接，最终回答由 FastReAct 无工具归纳产生；确定性摘要仅作为 fallback 候选。

### Deep：2024 vs 2025 营业收入比较

问题：`浏览器验收 Deep：比较海康威视2024年和2025年的营业收入变化，只给出两年数值、变化金额和同比百分比，并引用文件名。`

结果：

- 路径：`资料库检索 · 深入分析 · 深入回答`
- Scope：`海康威视年报` hard scope，5 sources，范围可检索。
- PSKA run：`askrun_27e6da07363f41ba8cdc387c91c766ba`
- FastReAct session：`pska:test_user_3:ask_bc2ffa72056943bf86e400b9978865fd`
- FastReAct protocol：`chat_completion_stream`
- UI 总耗时：约 26.9s。
- 答案：
  - 2024 年营业收入：924.96 亿元
  - 2025 年营业收入：925.08 亿元
  - 变化金额：增加 0.12 亿元
  - 同比增长：+0.01%

PSKA/FastReAct 对账：

| 项目 | 结果 |
| --- | --- |
| PSKA route | `retrieval_owner=fastreact_pska_mcp`, `selected_intent=deep` |
| Tool profile | `ask_read` |
| Tool policy | allowlist + hard KB/source scope |
| 允许工具 | `pska_pska_search`, `pska_pska_index_status`, `pska_pska_read_evidence_context`, `pska_pska_graph_context`, `pska_pska_digest_context` |
| Agentic budget | soft 4 / hard 12 |
| FastReAct events | 20 |
| Tool calls | 4 |
| Runtime status | completed |
| Control signal | 空 |
| `[STOPPED]` | 未出现 |
| 接收错误 | 未出现 |
| 最大单步 LLM usage | prompt 30561 / completion 1239 / total 31800 |

Deep 实际工具调用：

| # | Tool | Query / Args 摘要 |
| --- | --- | --- |
| 1 | `pska_pska_search` | `海康威视 2024年 营业收入 2025年 营业收入 同比` |
| 2 | `pska_pska_read_evidence_context` | 读取 `海康威视2025.pdf` 的收入构成表 chunk |
| 3 | `pska_pska_search` | `海康威视 2024 营业收入 合计 金额 亿元` |
| 4 | `pska_pska_read_evidence_context` | 读取 `海康威视2024.pdf` 候选 chunk |

评价：功能通过，但 citation precision 仍有瑕疵。答案数值来自 `海康威视2025.pdf` 的同一张跨年表，足以支持 2024/2025 对比；但 Citation Selection 额外挂了一条 `海康威视2024.pdf` 的递延所得税资产 chunk，属于 Evidence Composition/Citation 后处理需要继续收紧的问题。

## Fallback 与错误审计

| 项目 | 本轮状态 | 说明 |
| --- | --- | --- |
| Quick FastReAct prompt 截断 | 已修复 | durable run 输入保留长 query/history；本轮无 `[... truncated ...]` |
| Quick agentic synthesis | 通过 | `fastreact_agentic_service` 被选中 |
| Deep MCP bootstrap | 通过 | `/ready` 显示 PSKA MCP alive/loaded，10 tools |
| Deep 最大迭代停止 | 未出现 | 本轮 hard cap 12，实际 completed |
| max token | 未出现 | 最大单步 total tokens 31800，低于 128k context |
| 接收错误 | 未出现 | FastReAct/PSKA trace 均无 runtime error |
| BM25 缺失 fallback | 当前未触发 | `rank_bm25` 已安装且为核心依赖；代码 fallback 应显式 degraded |
| Vector fallback lexical | 当前未触发 | 仍应保持可观测 |
| Quick deterministic fallback | 当前未选中 | 仍作为备用候选保留 |
| Deep fallback Quick | 当前未触发 | UI/trace 应继续显式标降级 |

## 当前可用性判断

可以正常使用：

- 多 KB scope 选择与隔离。
- BM25 + BGE-M3 hybrid search。
- PDF 全文检索。
- 表格证据召回和行对齐。
- Quick 单事实问答。
- Deep 多步工具检索与跨年度比较。
- No-answer policy。

仍未收口：

- Citation Selection 需要避免“年份/主体命中但问题字段不相关”的 chunk 进入最终引用。
- Deep 需要进一步控制重复搜索和 token 成本，虽然本轮没有触顶。
- `/v1/runs` 列表接口需要分页/索引优化，否则历史日志增长后不适合作审计入口。

## 建议下一步

1. 在 Evidence/Citation Validator 中加入通用的 field relevance 校验，避免“年份+主体命中但字段不相关”的引用。
2. 在 service-check/readiness 中暴露 `rank_bm25_available`、`hybrid_vector_candidates_smoke`、`embedding_coverage_by_kb`。
3. 给 Deep 增加工具调用预算和 tool-result compaction，降低多轮问题的 token 成本。
4. 为 `/v1/runs` 增加分页索引或时间窗口过滤，避免审计接口被历史运行记录拖慢。
