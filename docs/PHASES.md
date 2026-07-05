# PSKA Phases

Status: current roadmap index on `tenant`
Last reviewed: 2026-07-05

This page is the one-page phase map for PSKA. It is intentionally short:
architecture details live in [Architecture](ARCHITECTURE.md), decisions live in
[ADRs](adr/README.md), and future stage changes live in RFCs.

## Phase Map

| Phase | Name | Status | Baseline |
| --- | --- | --- | --- |
| Phase 0 | Proof of Concept | Closed | Pre-multitenant RAG and ingest experiments |
| Phase 1 | Evidence QA Engine | Frozen | Architecture: `4d6308b6`, `v0.1.0-phase1-freeze`; acceptance: `3b0949e7`, `v0.1.0-phase1` |
| Phase 2 | Evidence Composition | RFC | [RFC 0002: Multi-evidence Composition](rfcs/0002-multi-evidence-composition.md) |
| Phase 3 | Agent Workflow | Not started | Future RFC |

## Phase 1: Evidence QA Engine

Phase 1 answered the question: can PSKA support a trustworthy, scoped,
evidence-driven RAG foundation?

Release notes: [v0.1.0 Phase 1 Evidence QA Engine](RELEASE_V0_1_PHASE1.zh.md).

The frozen Phase 1 pipeline is:

```text
Candidate Retrieval
  -> Evidence Scoring
  -> Evidence Validation
  -> Citation Selection
  -> Answer Pipeline
```

Frozen does not mean bug-free. It means future quality work should preserve the
stage boundaries and extend the system through generic scorers, validators,
selectors, extractors, prompt patterns, or RFC-approved stage changes.

### Exit Criteria

Phase 1 exit is based on an evidence pipeline baseline, not on every future QA
capability being complete.

| Area | Status | Acceptance meaning |
| --- | --- | --- |
| Multi-KB Isolation | Passed | One account can hold multiple KBs and switch active Ask scope. |
| KB Scope Isolation | Passed | Hard-scoped Ask/search stays inside the selected KB. |
| Tenant Isolation | Passed | Browser session uses AuthNode/Gateway tenant and user claims. |
| Hybrid Retrieval | Passed | KB search supports `hybrid` mode with local embedding configured. |
| PDF Retrieval | Passed | Search can retrieve relevant spans across ingested PDF chunks. |
| PDF Span Retrieval | Passed | Ask citations include source windows and source document metadata. |
| Structured Table QA | Passed | Numeric table answers can be extracted from PDF/table chunks. |
| Table Row Alignment | Passed | Table answers avoid cross-row contamination such as mixing revenue, profit, and R&D rows. |
| Citation Selection | Passed | Answers expose selected citations and cite inspectable spans. |
| No-answer Policy | Passed | Unsupported questions return evidence-insufficient/no-answer instead of fabricated citations. |
| Evidence Pipeline Audit | Passed | Failures can be localized to retrieval, evidence validation, citation, or answer stages. |
| Architecture Freeze | Passed | Phase 1 stage boundaries are documented as the architecture contract. |
| ADR | Passed | Core design decisions are recorded under `docs/adr/`. |
| RFC | Passed | Multi-evidence composition is deferred through RFC 0002 instead of retrieval rules. |

### Deferred Work

| Area | Status | Boundary |
| --- | --- | --- |
| Multi-evidence Composition | Phase 2 | Evidence Set and composition validators. |
| Deep Ask Workflow | Phase 2 | Bounded research loops on top of Evidence Composition. |
| Heavy Reranker | Future | Optional stage implementation after measured retrieval/ranking need. |
| Graph Retrieval | Future | Retrieval extension that must still return evidence/citation audit. |

## Phase 2: Evidence Composition

Phase 2 should not be framed as "Deep Ask first." The core problem is how
multiple evidence records become one trustworthy answer.

Phase 2 asks:

- when does a question require an Evidence Set rather than a single citation?
- how should Evidence Records be grouped, ordered, and validated?
- how should citations cover every value used in a comparison or calculation?
- how does Answer Pipeline consume composed evidence without changing Phase 1
  retrieval responsibilities?

Trend, comparison, aggregation, conflict, and version-aware questions belong
here. They should not be solved by adding domain-specific retrieval rules.

## Phase 3: Agent Workflow

Phase 3 is not started. Agent workflow should build on Evidence Composition
instead of bypassing it. Long-running planning, bounded research loops, tool
use, and memory updates should still return to evidence, citation, and answer
audit contracts before reaching users.
