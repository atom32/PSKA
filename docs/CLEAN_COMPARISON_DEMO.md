# Clean Comparison Demo Runbook

This runbook is for a clean PSKA versus GBrain/LLM_Wiki comparison and a short demo recording.

## Upstream Demo Data Signals

GBrain does not ship a single product demo corpus. It uses three kinds of public/demo-safe material:

- README production story: large private numbers and a sample query, but not a reusable corpus.
- Synthetic calibration fixtures: anonymous `alice-example`, `acme-example`, `widget-co` Markdown files plus expected gradeable claims.
- Public evals: LongMemEval and the sibling `gbrain-evals` corpus for retrieval/QA benchmarks.

Best reusable GBrain fixture paths:

- `test/fixtures/calibration/extract-takes-corpus/`
- `test/fixtures/calibration/holdout/`
- `test/fixtures/retrieval-quality/namedthing.jsonl`
- `test/fixtures/retrieval-quality/relational/`
- `test/fixtures/longmemeval-mini.jsonl`
- `docs/eval-bench.md`

For a recorded product demo, prefer the calibration Markdown fixtures. They are small, synthetic, and easy to explain on camera.

## Suggested Clean Corpus

Use a tiny source folder with mixed formats:

- 6-10 Markdown notes copied from GBrain calibration fixtures.
- 1 Excel workbook with two sheets: company pipeline and meeting actions.
- 1 PDF or DOCX only if you want to show optional document extraction.
- 2 near-duplicate notes that express the same claim in different words, to show `dedupe_key` behavior.

Suggested demo questions:

- "What should I know before meeting Alice?"
- "Which companies are connected to Acme Example?"
- "What claims or predictions were made about Charlie Example?"
- "What actions are open from the fundraising notes?"
- "What is in the Excel pipeline for Acme Example?"

## PSKA Clean Start

Destructive reset:

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
```

Configure `.pska/config.json` with a single clean files root, then sync:

```bash
./scripts/pska --config .pska/config.json files-sync
```

Build graph/claims:

```bash
./scripts/pska --config .pska/config.json extract-all --owner-user-id user_primary
```

Run direct QA/search:

```bash
./scripts/pska --config .pska/config.json search --query "What should I know before meeting Alice?" --top-k 8
```

If FastReAct is configured and online, run digest:

```bash
./scripts/pska --config .pska/config.json digest-now --force
```

## Repeat-Run Checks

Run sync twice. The second run should report unchanged files:

```bash
./scripts/pska --config .pska/config.json files-sync
./scripts/pska --config .pska/config.json files-sync
```

Run extraction/digest twice. With stable source refs and `dedupe_key`, repeated semantic candidates should upsert rather than duplicate.

## Demo Recording Beats

1. Show the clean corpus folder and highlight Markdown plus Excel.
2. Run `db-reset`, `files-sync`, and the first query.
3. Ask a graph-flavored question about people/companies.
4. Ask an Excel-specific question.
5. Run `files-sync` again and show unchanged/idempotent behavior.
6. Run or describe digest candidate writing with `dedupe_key`.
7. Close with a comparison: GBrain is strongest on markdown-first graph/retrieval discipline; LLM_Wiki is strongest on multi-format document extraction; PSKA now covers the key middle path for clean personal-knowledge demos.
