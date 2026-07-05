# Release Notes: v0.1.0 Phase 1 Evidence QA Engine

Status: local Phase 1 baseline on `tenant`
Date: 2026-07-05

`v0.1.0-phase1` 不是一次公网产品发布，而是 PSKA Phase 1 的可回归工程基线。它标志着 PSKA 从“能做 RAG 的原型”收敛为一条以 Evidence 为核心、可观测、可解释、可审计、可回归的问答流水线。

## Tags

| Tag | Meaning | Commit |
| --- | --- | --- |
| `v0.1.0-phase1-freeze` | Architecture Baseline：Phase 1 架构契约冻结点 | `4d6308b6` |
| `v0.1.0-phase1` | Validated Functional Baseline：真实浏览器验收后的功能基线 | `3b0949e7` |

两个 tag 都应保留。`phase1-freeze` 回答“架构边界是什么”，`phase1` 回答“这套架构是否通过真实数据验收”。

## Highlights

- Multi-KB architecture：一个账号可拥有多个知识库，并在 Ask 中切换 scope。
- KB scope isolation：Ask/search 支持 hard-scoped selected KB，不扩大到未选 KB。
- Hybrid retrieval：BM25 + local BGE-M3 embedding 的 hybrid candidate generation。
- Evidence Scoring Pipeline：通用确定性证据信号打分，避免继续堆题型规则。
- Evidence Validation：主体、scope、支撑性和 no-answer 证据校验。
- Citation Selection Pipeline：引用从 TopK 副产品变成独立决策，支持 selected span 和 feature audit。
- Answer Pipeline Audit：最终答案 owner、fallback、no-answer policy 可审计。
- Structured Table QA：支持 PDF/table chunk 中的数值抽取。
- Table Row Alignment：避免 cross-row contamination，例如把营业收入、净利润、研发投入比例串行混答。
- Browser-based validation：使用真实 PSKA Gateway/AuthNode 浏览器 session 和真实海康威视年报 PDF 验收。
- Architecture Contract、ADR、RFC：Phase 1 stage boundary、禁止领域捷径、RFC 0002 Multi-evidence Composition 已文档化。

## Browser Acceptance

验收环境：

- user: `tenant_graphintell / test_user_3`
- frontend: PSKA Gateway on `./start.sh`
- KB: `海康威视年报`
- source PDFs: `~/Downloads/海康威视2021.pdf` 到 `~/Downloads/海康威视2025.pdf`

验收结果：

| Area | Result | Evidence |
| --- | --- | --- |
| Multi-KB Isolation | Passed | 页面显示 `默认资料库` 与 `海康威视年报`，Ask scope 可切换。 |
| KB Scope Isolation | Passed | 选中 `海康威视年报` 后，Ask scope 显示 1 KB / 5 sources。 |
| Hybrid Retrieval | Passed | `/workspace/knowledge-bases/search` 返回 `mode: hybrid`，Top results 来自选中 KB。 |
| PDF Retrieval | Passed | 搜索命中 2025 年报的多个 PDF chunk，包括经营摘要、收入构成、附注和研发投入段。 |
| Structured Table QA | Passed | 2025 年营业收入答出 `92,507,796,069.94`。 |
| Table Row Alignment | Passed | 2025 Q4 营业收入答出 `26,750,013,612.93`，没有串到净利润或研发投入行。 |
| Citation Selection | Passed | 答案带可检查引用、source window 和 source document metadata。 |
| No-answer Policy | Passed | 问火星大气成分比例时返回 evidence-insufficient/no-answer，0 引用，不编造。 |

## Final Functional Fix

真实浏览器验收发现：

```text
Retrieval PASS
Evidence Scoring PASS
Evidence Validation FAIL
```

具体表现是：候选证据已经命中 `海康威视2025.pdf`，但中文紧连写法 `2025年海康威视营业收入` 被抽成过硬 compound anchor，导致 evidence validation 以 `missing_query_anchor` 丢弃候选。

修复提交：

- `3b0949e7 Fix PSKA CJK compound anchor validation`

修复原则：

- 不引入公司、行业、年报或 benchmark 特例。
- 将中文 compound anchor 拆成通用语言结构验证。
- 要求主体部分和指标部分共同命中，避免只有“营业收入”就放过其他主体。

## Verification

默认低成本回归：

```bash
PYTHONPATH=core/src .pska/venvs/pska-py312/bin/python -m pytest \
  core/tests/test_fastreact_integration.py \
  core/tests/test_product_flows.py \
  core/tests/test_citation_pipeline.py \
  core/tests/test_answer_pipeline.py \
  core/tests/test_embeddings.py -q
```

最近通过结果：

```text
190 passed
```

集成栈验收：

```bash
./start.sh
./scripts/pska --config .pska/config.json service-check
```

最近 `service-check`：

```json
{"ok": true, "health": true, "database": "postgresql:///pska"}
```

## Deferred To Phase 2 Or Later

| Area | Status | Boundary |
| --- | --- | --- |
| Multi-evidence Composition | Phase 2 | Evidence Set、slot coverage、composition validators。 |
| Deep Ask Workflow | Phase 2 | 基于 Evidence Composition 的 bounded research loop。 |
| Heavy Reranker | Future | 在 Hit@50 高但 TopK ranking 不足时评估 Cross Encoder 等实现。 |
| Graph Retrieval | Future | 可作为 retrieval extension，但必须返回 evidence/citation audit。 |

## Summary

Phase 1 的核心成果不是单个问题答对，而是 PSKA 建立了稳定的 Evidence QA Engine：

```text
Candidate Retrieval
  -> Evidence Scoring
  -> Evidence Validation
  -> Citation Selection
  -> Answer Pipeline
```

后续 Phase 2 应围绕 Evidence Composition 展开，而不是回到 Retriever 里继续加题型规则。
