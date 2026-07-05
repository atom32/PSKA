# ADR 0002: No Domain-specific QA Shortcuts

Status: Accepted
Date: 2026-07-05

## Context

Golden evaluation sets are useful because they expose recurring failures. They
also create a risk: the system can appear to improve by adding shortcuts keyed
to a sample company, sample report, sample wording, or one benchmark question.
That would make PSKA less reliable outside the test corpus.

## Decision

PSKA is a domain-agnostic knowledge system. QA quality logic must not depend on
a specific industry, company, document title, fixture, or question type.

Allowed patterns:

- generic scorers, validators, selectors, and extractors
- metadata-aware policy that applies across domains
- prompt examples that illustrate behavior without routing on sample content
- tests that use concrete fixtures to verify generic behavior

Disallowed patterns:

- `if company == ...`
- `if "specific benchmark phrase" in query`
- corpus-specific boosting or suppression
- retrieval rules that solve one trend/comparison question shape by name

## Alternatives Considered

- Accept fixture-specific boosts to improve immediate benchmark numbers.
- Keep a hidden allowlist of sample entities or document names.

## Consequences

- Benchmark gains may be slower but should generalize better.
- Code review can reject shortcuts by referencing this ADR instead of debating
  style preferences.
- Multi-evidence composition and Deep Ask must be built as general abilities,
  not as retrieval special cases.
