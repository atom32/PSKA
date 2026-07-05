# ADR 0003: Hybrid Retrieval Before Heavy Reranking

Status: Accepted
Date: 2026-07-05

## Context

BM25 alone was not enough for stable table-heavy document QA. Adding local
BGE-M3 embeddings and hybrid merge improved candidate recall on the Golden Eval
set. The main remaining failures moved from "cannot find the evidence" toward
"evidence is present but must be ranked, selected, or composed correctly."

## Decision

Phase 1 uses hybrid candidate generation plus deterministic evidence scoring
before adopting heavier rerankers. A Cross Encoder or external rerank service
should be evaluated only when the retrieval audit shows high candidate recall
but weak top-k ranking, for example high `Hit@50` with insufficient `Hit@5`.

The default regression path should remain low-cost and local-first:

- BM25 and local embedding retrieval
- deterministic evidence scoring
- Golden Eval metrics without paid LLM calls

## Alternatives Considered

- Add a Cross Encoder immediately.
- Tune BM25-only ranking further.
- Optimize answer prompts before retrieval metrics were observable.

## Consequences

- Retrieval decisions are based on measurable `Hit@K` deltas.
- Local BGE-M3 is the default embedding path; vLLM or hosted services can be
  configured when deployment conditions justify them.
- Heavy reranking remains a replaceable stage implementation, not a hard
  dependency of PSKA's core architecture.
