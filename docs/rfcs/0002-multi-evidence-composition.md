# RFC 0002: Multi-evidence Composition

Status: Draft
Phase: 2
Date: 2026-07-05

## Problem

Phase 1 Quick Ask is strongest when a question can be answered from one or a
small number of directly relevant citations. Trend, comparison, aggregation,
and "latest across versions" questions often need an Evidence Set: multiple
documents, years, tables, or spans that must be selected together before the
answer can be computed or synthesized.

Examples include:

- compare values across years
- calculate percentage change
- prefer the latest version while keeping older evidence available
- reconcile conflicting documents

These cases should not be solved by adding retrieval rules for specific
question text. They require a general composition layer above Phase 1.

## Goals

- Represent an Evidence Set as a first-class object.
- Decide when a query needs single-evidence QA versus multi-evidence
  composition.
- Preserve tenant, user, KB, document, and citation scope.
- Let Citation Selection and Answer Pipeline consume composed evidence without
  breaking existing Phase 1 contracts.
- Keep the default regression path measurable without paid LLM calls.

## Non-goals

- Hardcode company, industry, annual-report, or benchmark-specific logic.
- Replace the Phase 1 Retrieval -> Evidence -> Citation -> Answer pipeline.
- Require Deep Ask for every comparison or aggregation question.
- Make retrieval responsible for planning.

## Proposed Shape

```text
User Query
  -> Query Intent / Composition Need Detection
  -> Candidate Retrieval
  -> Evidence Scoring
  -> Evidence Validation
  -> Evidence Set Builder
  -> Citation Selection per Evidence Set member
  -> Answer Pipeline with composition validators
  -> Final Answer
```

## Core Data Structures

`EvidenceRecord` should carry one validated citation candidate:

- tenant/user/KB/document/chunk identifiers
- retrieval rank and score
- evidence scoring features
- validation status and reasons
- citation selection score
- selected span
- source metadata such as date/version when available

`EvidenceSet` should carry a composed group:

- set id
- query requirement it satisfies
- ordered records
- composition key, such as time, version, entity, region, or source type
- missing slots
- conflicts
- calculation inputs
- audit timeline

## Recognition

The first implementation should detect multi-evidence need with generic
signals:

- comparative language
- temporal or version range
- aggregation verbs
- multiple requested entities or dimensions
- explicit "latest", "previous", "change", "compare", or "between" semantics

These signals may be implemented as prompt patterns or validators, but they
must not branch on sample company names, sample documents, or benchmark ids.

## Compatibility With Phase 1

Phase 1 remains valid for single-evidence questions. Multi-evidence
composition adds an optional Evidence Set stage after validation and before
final answer ownership.

Existing Citation Selection can be reused per EvidenceRecord. Answer Pipeline
should gain validators that understand Evidence Sets, such as:

- required slot coverage
- numeric consistency
- citation coverage for each computed value
- no-answer when required slots are missing

## Acceptance Criteria

- Single-evidence Golden Eval metrics do not regress.
- Trend/comparison questions report whether required evidence slots were found.
- Final answers cite every value used in a calculation.
- No domain-specific shortcuts are introduced.
- Retrieval-only evaluation can measure Evidence Set slot recall without
  calling a paid LLM.
