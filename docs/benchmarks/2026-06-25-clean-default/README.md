# PSKA clean default benchmark - 2026-06-25

本记录用于复现一次干净 default workspace 的 PSKA ingest / extraction / search benchmark。执行时 PSKA 服务已停止，本次只使用 CLI 和 PostgreSQL。

## 环境

路径已脱敏为占位变量；复现时按本机环境设置：

```bash
export PSKA_REPO="/path/to/pska"
export PSKA_WORKSPACE="/path/to/PSKA_workspaces/default"
```

- Repo: `$PSKA_REPO`
- Branch: `master`
- Repo HEAD before benchmark notes were written: `f2f6fd7d11683a9ab1da3eaae9839a3f1bc79193`
- Workspace: `$PSKA_WORKSPACE`
- Notes root: `$PSKA_WORKSPACE/notes`
- Twitter archive root: `$PSKA_WORKSPACE/twitter_archive`
- Config: `.pska/config.json`
- Database: `postgresql:///pska`
- LLM key source used for extraction: `PSKA_LLM_API_KEY_FILE=~/api_key.txt`

## Corpus

Corpus root:

```bash
$PSKA_WORKSPACE/notes/benchmark-2026-06-25
```

Contents:

- 13 synthetic Markdown fixtures copied from GBrain calibration data.
- 2 PSKA-specific Markdown notes for repeated semantic claim / dedupe behavior.
- 1 generated XLSX workbook for spreadsheet ingestion coverage.

GBrain source used:

```bash
/tmp/codex-compare-gbrain/test/fixtures/calibration
git -C /tmp/codex-compare-gbrain rev-parse HEAD
# 814258dda67945ffec9457a1e73980e947b7e462
git -C /tmp/codex-compare-gbrain remote get-url origin
# https://github.com/garrytan/gbrain.git
```

The GBrain fixture README describes this calibration corpus as synthetic regression data for the `extract-takes` prompt, not private brain content.

To recreate the corpus after cloning GBrain fixtures:

```bash
git clone https://github.com/garrytan/gbrain.git /tmp/codex-compare-gbrain
git -C /tmp/codex-compare-gbrain checkout 814258dda67945ffec9457a1e73980e947b7e462
python3 docs/benchmarks/2026-06-25-clean-default/prepare_corpus.py \
  --gbrain-fixtures-root /tmp/codex-compare-gbrain/test/fixtures/calibration \
  --notes-root "$PSKA_WORKSPACE/notes"
```

`prepare_corpus.py` recreates only `notes/benchmark-2026-06-25`.

## Command Log

All raw command outputs are under:

```bash
docs/benchmarks/2026-06-25-clean-default/outputs/
```

Initial destructive reset and ingest:

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
./scripts/pska --config .pska/config.json files-sync
./scripts/pska --config .pska/config.json files-sync
```

Extraction attempts:

```bash
./scripts/pska --config .pska/config.json extract-all --owner-user-id user_primary
PSKA_LLM_API_KEY_FILE=~/api_key.txt ./scripts/pska --config .pska/config.json extract-all --owner-user-id user_primary
```

The first extraction failed because `extract-all` did not pick up the LLM key from `.pska/config.json`; it required an env var or key-file env var. The second attempt exposed common LLM schema drift: string confidence values such as `"high"` and a string `review_items[].proposal`. That compatibility issue was patched in `core/src/pska_core/extraction.py`, then the clean run below was executed.

Clean run after schema compatibility fix:

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
./scripts/pska --config .pska/config.json files-sync
PSKA_LLM_API_KEY_FILE=~/api_key.txt ./scripts/pska --config .pska/config.json extract-all --owner-user-id user_primary
psql postgresql:///pska -X -A -F "|" -c '<counts query>'
psql postgresql:///pska -X -A -F "|" -c '<xlsx extraction sample query>'
./scripts/pska --config .pska/config.json files-sync
```

Search queries:

```bash
./scripts/pska --config .pska/config.json search --query "What should I know before meeting Alice Example?" --top-k 8
./scripts/pska --config .pska/config.json search --query "What is in the Excel pipeline for Acme Example?" --top-k 8
./scripts/pska --config .pska/config.json search --query "What claims or predictions were made about Charlie Example?" --top-k 8
./scripts/pska --config .pska/config.json search --query "Which companies are connected to Acme Example?" --top-k 8
./scripts/pska --config .pska/config.json search --query "What actions are open from the fundraising notes?" --top-k 8
```

Verification:

```bash
cd core
/usr/bin/time -p python3 -m pytest -q
```

## Results

Clean `files-sync`:

- `scanned`: 16
- `ingested`: 16
- `twitter_imported`: 0
- `failed`: 0

Repeat `files-sync` after extraction:

- `scanned`: 16
- `ingested`: 0
- `unchanged_files`: 16
- `twitter_imported`: 0
- `failed`: 0

Database counts after successful extraction:

| Table | Count |
| --- | ---: |
| `source_items` | 16 |
| `documents` | 16 |
| `chunks` | 28 |
| `entities` | 90 |
| `hyperedges` | 35 |
| `knowledge_claims` | 170 |
| `review_items` | 31 |

XLSX ingestion sample:

- `title`: `portfolio-pipeline.xlsx`
- `extractor`: `xlsx-zip-xml`
- `sheet_count`: 2
- Sheets: `Pipeline` with 4 rows / 5 columns, `Actions` with 4 rows / 4 columns.

Search timings are CLI wall time, including process startup:

| Query | Time | Top result |
| --- | ---: | --- |
| What should I know before meeting Alice Example? | 1.93s | `portfolio-pipeline.xlsx` |
| What is in the Excel pipeline for Acme Example? | 2.03s | `decision-log-2025-q3.md` |
| What claims or predictions were made about Charlie Example? | 2.15s | `portfolio-pipeline.xlsx` |
| Which companies are connected to Acme Example? | 1.92s | `meeting-2026-04-17-hiring-charlie-example.md` |
| What actions are open from the fundraising notes? | 1.97s | `essay-cities-and-ambition.md` |

The direct CLI search latency is roughly 1.9-2.2s on this corpus. Several top results are not ideal semantically, so this run is also a useful baseline for ranking and graph boost tuning, not just latency.

## Demo Fix Verification

After the initial benchmark, the Excel demo query exposed two demo-quality issues:

- Agentic Graph Path could surface a FastReAct answer claiming the query was truncated.
- Spreadsheet-specific queries did not reliably rank the XLSX source first.

Follow-up verification outputs:

```bash
docs/benchmarks/2026-06-25-clean-default/outputs/18-search-excel-acme-after-ranking-fix.txt
docs/benchmarks/2026-06-25-clean-default/outputs/19-graph-qa-excel-deterministic-after-ranking-fix.txt
docs/benchmarks/2026-06-25-clean-default/outputs/20-core-tests-after-demo-fix.txt
```

Current result for the Excel demo query:

- `search --query "What is in the Excel pipeline for Acme Example?" --top-k 5`: top result is now `portfolio-pipeline.xlsx`, with `spreadsheet_intent_match`.
- `graph-qa-eval --mode deterministic --question "What is in the Excel pipeline for Acme Example?" --summary`: passed, 8 citations, 8 supporting passages, 8 graph paths, 1035 answer chars.
- Test result after the fix: `309 passed in 18.68s`.

Follow-up verification for the Chinese pipeline next-step demo:

```bash
docs/benchmarks/2026-06-25-clean-default/outputs/21-graph-qa-acme-pipeline-next-step-after-table-answer-fix.txt
docs/benchmarks/2026-06-25-clean-default/outputs/22-core-tests-after-table-answer-fix.txt
```

Current deterministic answer for `Acme Example 当前 pipeline 里的下一步行动是什么？` now extracts the table row directly:

```text
Acme Example 当前 pipeline 记录的负责人是 Alice Example，状态是 active，ARR 是 1200000，下一步行动是：Prepare partner meeting brief。
```

Test result after this table-answer fix: `311 passed in 18.52s`.

Test result:

```text
307 passed in 17.70s
```
