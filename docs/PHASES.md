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
| Phase 1 | Evidence QA Engine | Frozen | `4d6308b6`, `v0.1.0-phase1-freeze` |
| Phase 2 | Evidence Composition | RFC | [RFC 0002: Multi-evidence Composition](rfcs/0002-multi-evidence-composition.md) |
| Phase 3 | Agent Workflow | Not started | Future RFC |

## Phase 1: Evidence QA Engine

Phase 1 answered the question: can PSKA support a trustworthy, scoped,
evidence-driven RAG foundation?

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
