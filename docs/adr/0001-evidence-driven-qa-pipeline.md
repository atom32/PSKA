# ADR 0001: Evidence-driven QA Pipeline

Status: Accepted
Date: 2026-07-05

## Context

Early PSKA Ask work treated retrieval, evidence filtering, citation choice, and
answer generation as one broad RAG flow. That made it hard to explain whether a
bad answer came from candidate generation, evidence quality, citation choice,
or final answer synthesis.

The Phase 1 work split this flow into explicit stages:

```text
Candidate Retrieval
  -> Evidence Scoring
  -> Evidence Validation
  -> Citation Selection
  -> Answer Pipeline
```

## Decision

PSKA Quick Ask is an evidence-driven QA engine. Each stage has a bounded
responsibility, produces audit output, and should be tested independently.
Quality improvements should extend a stage through registered scorers,
validators, selectors, extractors, or compatible prompt patterns.

## Alternatives Considered

- Keep a monolithic retrieval-plus-prompt function.
- Continue adding ad hoc conditions inside retrieval and answer assembly.
- Move all reasoning into a long-chain agent before stabilizing evidence.

## Consequences

- Failures can be localized to Retrieval, Evidence, Citation, or Answer.
- UI explainability can expose the same audit artifacts used by tests.
- Future Cross Encoder, Graph, or Agent workflows can plug into stage contracts
  rather than replace the whole Ask path.
- Stage boundary changes require an RFC because they affect regression,
  observability, and product behavior.
