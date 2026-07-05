# ADR 0004: Answer Pipeline Audit

Status: Accepted
Date: 2026-07-05

## Context

Final answers may come from FastReAct synthesis, deterministic extraction, or a
no-answer policy. Previously, fallback behavior could be hidden in conditional
logic, making it hard to explain why a synthesized answer was rejected or why a
fallback was selected.

## Decision

Final answer ownership is decided by `AnswerPipeline`. Candidate answers are
validated by named validators and the selected owner is exposed in audit output.
Fallbacks and no-answer decisions are state transitions, not silent branches.

## Alternatives Considered

- Let the LLM always decide whether its answer is good enough.
- Keep deterministic fallback as an implicit branch in the API handler.
- Return both answers and let the UI choose.

## Consequences

- Quick Ask can explain whether the final owner is FastReAct synthesis,
  deterministic fallback, or no-answer policy.
- Missing value coverage, raw evidence leakage, and future consistency checks
  can be added as validators.
- The answer layer can mature without changing retrieval or citation
  contracts.
