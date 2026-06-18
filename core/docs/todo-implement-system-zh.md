# PSKA Todo Implement System

日期：2026-06-18

## 目标

这个文档把 PSKA 的推进方式固定下来：后续 coding agent 不应依赖用户不断说“继续”。它应该能从结构化 TODO 中选择下一项 ready task，执行实现，跑门禁，提交证据，并更新任务状态。

自动化 v1 是“建议下一个任务 + 明确门禁”，不是全自动改代码机器人。代码修改仍由当前 coding agent 在明确任务范围内完成。

## 任务格式

每个 TODO 必须包含：

```yaml
id: HW-001
track: Human Workflow
priority: P0
status: ready
user_value: 用户每天能知道 PSKA 现在有什么、该处理什么、哪里坏了。
dependencies: []
implementation_hint: 复用 mvp-status --summary、review-list、jobs stats，先做 deterministic 输出。
open_source_candidates:
  - Rich
  - Typer
acceptance_gates:
  - 命令能在当前 Postgres 样例库上输出 service/job/review/digest/source 摘要。
  - 输出不依赖 LLM。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: ""
```

## 状态流转

- `backlog`：想做但还不能直接开始。
- `ready`：依赖清楚、验收清楚，可以实施。
- `in_progress`：当前 agent 正在做。
- `blocked`：遇到明确阻塞，必须写原因和下一步。
- `implemented`：代码或文档已完成，但门禁尚未全部通过。
- `verified`：门禁通过，有 evidence。
- `shipped`：已提交并可作为历史完成项。

## 自动选择规则

选择下一项任务时按以下规则：

1. 只选择 `status: ready`。
2. 优先当前产品轨道：`Human Workflow`。
3. 不跨过未满足依赖。
4. 同优先级时选用户价值更直接、验收更明确、改动面更小的任务。
5. 每项任务执行前先确认是否有成熟开源库或已有项目能力可复用。
6. FastReAct 与 PSKA 边界不混写：PSKA 不 import FastReAct internals，FastReAct 不直接访问 PSKA DB。
7. 失败门禁必须留下 blocked/implemented 状态和原因，不伪造完成。

## 完成规则

- 代码改动必须有测试或明确 smoke gate。
- 文档改动必须更新 roadmap 或 MVP scope 中的入口链接。
- 每个任务独立 commit。
- 不为了继续推进跳过失败测试。
- 新增长期设计决策时，需要同步更新产品设计或架构状态文档。
- 新增非核心能力时，必须记录 open-source-first 评估。

## 当前 Human Workflow Backlog

状态：`HW-001` 到 `HW-008` 已完成并验证。下一轮 Human Workflow backlog 已根据产品设计和架构状态生成，当前可从 `HW-009` 开始自动选择。

### HW-001 Daily Status Entry

```yaml
id: HW-001
track: Human Workflow
priority: P0
status: verified
user_value: 用户每天能从一个入口知道 PSKA 是否健康、有什么新内容、该处理什么。
dependencies: []
implementation_hint: 设计并实现每日使用入口，优先复用 mvp-status --summary、review-list、jobs stats；第一版可扩展现有 CLI 输出，不依赖 LLM。
open_source_candidates:
  - Rich
  - Typer
acceptance_gates:
  - 输出包含 service readiness、source/chunk counts、digest backlog、pending reviews、failed jobs、recommended commands。
  - 能在当前 Postgres 样例数据上运行。
  - 不需要 FastReAct 在线。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented deterministic `pska-core daily-status` entry. Verification on 2026-06-17: core pytest `184 passed in 8.57s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke `cd core && ../.pska/venvs/pska-py312/bin/python -m pska_core.cli daily-status` returned ok=true with database/schema/mcp/jobs/metrics readiness true, source_items=24, chunks=135, digest_backlog.jobs=1, pending_reviews.total_matching=0, failed_jobs.count=2, recommended_commands populated, and requires_fastreact_online=false while fastreact_ok=false."
```

### HW-002 Review Summary

```yaml
id: HW-002
track: Human Workflow
priority: P0
status: verified
user_value: 用户能快速判断 pending review 里哪些值得处理，哪些可以拒绝或延后。
dependencies: []
implementation_hint: 增强 review-list 摘要，展示 type、confidence、source refs、created_at、推荐操作和可 apply 状态。
open_source_candidates:
  - Rich
  - JSON Patch
acceptance_gates:
  - pending review 摘要能区分 memory_candidate、profile_update、relationship_candidate、action_candidate、conflict、low_confidence。
  - 每条 review 能显示 source refs 或明确标记缺失出处。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented enhanced `review-list --summary` rows with review_type, confidence, source_refs/source_ref_status, created_at, recommended_actions, apply_supported, and can_apply_now. Verification on 2026-06-17: targeted CLI tests `23 passed in 0.21s`; core pytest `185 passed in 8.50s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke `./scripts/pska review-list --status pending --owner-user-id user_primary --limit 5 --summary` returned successfully with count=0,total_matching=0 because the current sample DB has no pending review items. Unit coverage distinguishes memory_candidate, profile_update, relationship_candidate, action_candidate, conflict, and low_confidence, including present vs missing source refs."
```

### HW-003 Memory/Profile Read-only View

```yaml
id: HW-003
track: Human Workflow
priority: P1
status: verified
user_value: 用户能检查 PSKA 长期记忆和 profile 里已经相信了什么。
dependencies:
  - HW-002
implementation_hint: 增加 memory-list/profile-list 或等价只读 CLI，显示 confidence、source refs、last_verified_at、status。
open_source_candidates:
  - Rich
acceptance_gates:
  - 能列出 agent memories 和 profile cards。
  - 不修改任何长期记忆。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented read-only `memory-list` and `profile-list` CLI entries. Verification on 2026-06-17: targeted CLI tests `24 passed in 0.21s`; core pytest `186 passed in 8.00s`; twitter-x pytest `9 passed in 0.02s`. Sample Postgres data was prepared by adding one profile card with `./scripts/pska profile-propose ... --confidence 0.72`; existing agent memories were present. Sample smoke `./scripts/pska memory-list --owner-user-id user_primary --limit 3` returned count=3 agent memories with confidence/source_refs/status/read_only=true; `./scripts/pska profile-list --owner-user-id user_primary --limit 3` returned count=1 profile card with confidence=0.72, source_ref_status=present, status=active, read_only=true. The list commands only call store list methods and do not invoke MemoryService write/update paths."
```

### HW-004 Deterministic Daily Briefing v0

```yaml
id: HW-004
track: Human Workflow
priority: P1
status: verified
user_value: 用户每天看到一个不依赖 LLM 的 briefing，知道新资料、待处理、失败任务和下一步建议。
dependencies:
  - HW-001
  - HW-002
implementation_hint: 汇总新 source、digest backlog、pending review、failed jobs、connector state、recommended next commands。
open_source_candidates:
  - Rich
  - Typer
acceptance_gates:
  - FastReAct 离线时仍可输出。
  - 输出包含 deterministic next actions。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented deterministic `daily-briefing` CLI entry that assembles service readiness, recent sources, connector state, digest backlog, pending reviews, failed jobs, deterministic_next_actions, and recommended_commands without LLM use. Verification on 2026-06-17: targeted CLI tests `25 passed in 0.21s`; core pytest `187 passed in 8.03s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke `./scripts/pska daily-briefing --owner-user-id user_primary --limit 3` returned ok=true, source_items=24, chunks=135, recent_sources=3, connector source_channels=[files,manual,manual_canary], digest_backlog.jobs=1, pending_reviews.total_matching=0, failed_jobs.count=2, deterministic_next_actions populated. Offline FastReAct smoke with `PSKA_FASTREACT_URL=http://127.0.0.1:9` returned ok=true, requires_fastreact_online=false, fastreact_ok=false, and deterministic_next_actions still populated."
```

### HW-005 FastReAct Narrative Briefing

```yaml
id: HW-005
track: Human Workflow
priority: P2
status: verified
user_value: 用户获得更自然的每日总结，但保留 deterministic fallback。
dependencies:
  - HW-004
implementation_hint: 将 deterministic briefing context 交给 FastReAct 生成 narrative summary；PSKA 保存 answer/citations/trace summary。
open_source_candidates:
  - FastReAct existing skills
acceptance_gates:
  - FastReAct down 时 fallback 正常。
  - narrative briefing 的 source refs 被保存。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented optional `daily-briefing --narrative` FastReAct path. Default `daily-briefing` remains deterministic; `--narrative` sends deterministic context/source_refs to FastReAct and, on success, saves the answer as a `pska_briefing`/`daily_narrative` source item with purpose, represented_user_id, source_refs, trace_summary, and the FastReAct response in source metadata. FastReAct failure returns deterministic fallback with narrative.ok=false and does not save fake narrative data. Verification on 2026-06-17: targeted CLI tests `27 passed in 0.23s`; core pytest `189 passed in 8.55s`; twitter-x pytest `9 passed in 0.02s`; sample fallback smoke `PSKA_FASTREACT_URL=http://127.0.0.1:9 ./scripts/pska daily-briefing --owner-user-id user_primary --limit 2 --narrative` returned ok=true, narrative.attempted=true, narrative.ok=false, narrative.fallback=true, source_refs populated, and deterministic_next_actions populated. Unit coverage verifies successful narrative save with source_refs and trace_summary. Follow-up after real local timeout: added `--narrative-timeout-seconds`, corrected `requires_llm/requires_fastreact_online` for narrative mode, changed FastReAct chat purpose to `pska_narrative_briefing`, and compressed the prompt to short facts so it does not enter the heavy daily_briefing/tool path. Reverified targeted CLI `27 passed in 0.21s`, core `189 passed in 8.54s`, twitter-x `9 passed in 0.02s`, and real local smoke `./scripts/pska daily-briefing --narrative --narrative-timeout-seconds 30 --limit 2` returned narrative.ok=true, fallback=false, tool_calls=[], source_refs populated, and saved_source_item_id=src_9bba2746483757c3a29ed0f52f824ad6."
```

### HW-006 Agent Conversation Capture

```yaml
id: HW-006
track: Human Workflow
priority: P1
status: verified
user_value: PSKA 调用 FastReAct 产生的重要对话、答案、引用和 trace summary 能回流为可检索资料。
dependencies: []
implementation_hint: 新增或复用 source material 类型，保存 PSKA-originated agentic QA/digest/briefing result；避免保存 FastReAct 内部私有对象。
open_source_candidates:
  - Pydantic
  - JSONL tracing conventions
acceptance_gates:
  - agentic answer 可带 citations 存回 PSKA source material。
  - 存档记录包含 purpose、represented user、source refs、trace summary。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented reusable `capture_agent_conversation` helper and `agentic-search --capture`. Captures PSKA-originated prompt/answer as standard conversation source material with citations/source_refs and trace_summary, while avoiding full FastReAct private response persistence. HW-005 narrative save now reuses the same capture path. Verification on 2026-06-17: targeted tests `31 passed in 0.23s`; core pytest `190 passed in 8.57s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke wrote `source_item_id=src_f62eaf0593c75d46be625e667b777d45` with source_channel=pska_agent, record_type=conversation, purpose=hw006_smoke_agentic_answer, source_refs=[src_537cf3713a1a525995179d0f930d6fb1], trace_summary.evidence_check=has_citations."
```

### HW-007 Grounded Graph Candidate Review

```yaml
id: HW-007
track: Human Workflow
priority: P1
status: verified
user_value: 进入图谱的是有出处、有关系语义、有置信度的关联数据，而不是孤立结论。
dependencies:
  - HW-002
implementation_hint: 完善 relationship_candidate review/apply，要求 source refs、relation type、members、confidence、review status。
open_source_candidates:
  - Pydantic
  - JSON Schema
acceptance_gates:
  - 缺 source refs 的 graph candidate 不能直接 apply。
  - apply 后 hyperedge 保留 evidence/source refs 和 audit event。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented relationship_candidate review apply path with required relation_type, source_refs, members, confidence validation. Missing source_refs cannot apply and leaves review approved, not applied. Successful apply creates a hyperedge preserving evidence_text, source_refs, confidence, directionality/members, and records created_hyperedge_id/source_refs in review.apply audit metadata. Verification on 2026-06-17: targeted review/CLI tests `33 passed in 0.22s`; core pytest `192 passed in 8.59s`; twitter-x pytest `9 passed in 0.03s`; sample Postgres smoke applied `rev_hw007_relationship_smoke`, created hyperedge `hed_b46ae1a1872055738f787210b9a3e054`, preserved source_ref `src_31dad8d7fe855a06b0d4bf8072403c4c`, and audit decisions were approved/applied."
```

### HW-008 Digest Budget Policy

```yaml
id: HW-008
track: Human Workflow
priority: P1
status: verified
user_value: 空闲 digest 能重新联想相关资料，但不会无限重复处理或失控消耗 token。
dependencies: []
implementation_hint: 在现有 quota window 基础上明确 token、frequency、dedupe、max source/chunk set、similarity/tag/entity trigger 策略。
open_source_candidates:
  - tiktoken
  - tokenizers
acceptance_gates:
  - digest scheduler 能解释为什么某批资料被选中或跳过。
  - 已成功且无新关联触发的资料不会被无限重复排队。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented digest scheduler budget/explanation policy. `schedule_digest` now returns a policy object plus selected/skipped source item explanations, dedupes active/succeeded/failed/canceled digest-covered sources unless force=true, explains limit_reached selections, preserves quota window limits, and documents current trigger/token boundaries. Successful digest-covered sources are skipped until force=true or a future trigger policy selects them, preventing infinite repeat scheduling. Verification on 2026-06-17: targeted digest tests `3 passed in 0.10s`; core pytest `193 passed in 8.52s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke `./scripts/pska digest-schedule --owner-user-id user_primary --limit 2` created a digest job for 2 sources and returned selected_reasons=[new_or_triggered_source], skipped_reasons=[active_digest_job,completed_digest_job,limit_reached], with policy fields for dedupe, successful_source_repeat, failed_source_repeat, frequency, max_source_items=2, max_source_items_per_job=20, token_budget, trigger_policy, and force=false."
```

### HW-009 Digest E2E Write-back Gate

```yaml
id: HW-009
track: Human Workflow
priority: P0
status: verified
user_value: 用户能确认空闲 digest 不只是排队，而是真的从有限资料生成有出处的候选、review 或 memory/profile 写回，并正确完成或失败。
dependencies:
  - HW-006
  - HW-008
implementation_hint: 增加一个可重复的 digest E2E gate/smoke，优先复用现有 digest-schedule、job lease/context、FastReAct worker command、candidate write-back 和 job complete/fail；不要在 PSKA 内重写 agent loop。gate 应能在当前 postgresql:///pska 样例库上输出每一步的 job_id、source_refs、candidate/review counts、completion status 和失败诊断。
open_source_candidates:
  - pytest
  - Pydantic
  - FastReAct existing worker/event stream
acceptance_gates:
  - 命令或测试能解释 digest job 从 schedule 到 worker write-back 到 complete/fail 的全链路状态。
  - 产生的 candidate/review/memory/profile/hyperedge 结果必须带 source_refs；缺 source_refs 的写回被拒绝或进入 review。
  - FastReAct 不可用时 gate 给出明确诊断，不把 timeout 伪装成成功。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented repeatable `core/scripts/digest_e2e_gate.py` for the digest write-back gate. The gate creates a canary source, schedules a forced scoped `digest_via_fastreact` job, leases it as a worker, verifies batch context/chunks, confirms missing `source_refs` candidate write-back is rejected, writes grounded entity/review/agent-memory candidates with `source_refs`, completes the job with result metadata, and separately exercises a non-retryable fail path. It reports `contract.ok` separately from `fastreact.ok`, so FastReAct offline/auth failures are explicit diagnostics and strict runs can use `--require-fastreact-online`. Also fixed `job-worker --exclude-job-type` to actually pass exclusions into `JobService`, keeping the local worker from claiming digest jobs reserved for FastReAct. Verification on 2026-06-18: targeted gate/jobs/CLI/daemon/candidate tests `50 passed in 0.35s`; core pytest `196 passed in 8.67s`; twitter-x pytest `9 passed in 0.03s`; sample Postgres smoke `cd core && ../.pska/venvs/pska-py312/bin/python scripts/digest_e2e_gate.py --database-url postgresql:///pska --fastreact-timeout-seconds 1` returned ok=true with all contract checks true, source_item_id=src_678b2cfbbbe45f60a130dbd2efe239c1, job_id=3d3b4482-f19e-4734-9d9d-5a76f40f21f8, review_item=rev_5e37b140f3225ba0a090a7d72c71522a, agent_memory=agm_404be4b638d7475c9b9117021f56b318, failed diagnostic job_id=979f6065-9a07-42ce-8d73-ff5b3d4ec6f1, and fastreact.ok=false with explicit `Fastreact GET /ready failed with HTTP 401: service token required` diagnostic rather than pretending real FastReAct worker success."
```

### HW-010 Review Batch Operations

```yaml
id: HW-010
track: Human Workflow
priority: P1
status: verified
user_value: 用户能一次性处理低风险、同类型、出处完整的 review items，而不是逐条机械确认。
dependencies:
  - HW-002
  - HW-007
implementation_hint: 增加 review batch approve/reject/snooze 或等价 CLI/API；只允许对同 owner、同 review_type、source_ref_status=present、can_apply_now=true 的安全集合批量 apply，高影响 action 仍需单条确认。输出 dry-run summary、affected ids、skipped reasons 和 audit events。
open_source_candidates:
  - Rich
  - JSON Patch
  - Pydantic
acceptance_gates:
  - batch dry-run 能列出将处理、将跳过和原因。
  - batch apply 后每条 review 都有 audit event，且 apply 结果保留 source_refs。
  - 不满足安全条件的 review 不会被批量 apply。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `review-batch` CLI for dry-run-first batch review operations. It supports `approve`, `reject`, and `apply`, accepts explicit review IDs or owner/type/status filters, reports selected/to_process/skipped/affected counts, returns skipped reasons, and only mutates when `--execute` is present. Batch apply is constrained to approved items with source_refs, apply_supported/can_apply_now, same owner, same review_type, and safe apply review types (`profile_update`, `relationship_candidate`); high-impact/share/action/unsupported items remain single-item flows. Execution reuses `ReviewService` single-item methods, so each changed item emits the existing audit event and apply output keeps source_refs. Verification on 2026-06-18: targeted CLI/review tests `36 passed in 0.22s`; core pytest `199 passed in 8.63s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke on `postgresql:///pska` created `rev_16527d3671ff48d896564a2a420fb60f`, dry-run approve reported to_process=1/affected=0, execute approve produced audit `aud_f57994bff3d74bd996811cc60588b48f`, execute batch apply produced audit `aud_37cf59ad04754bb999ee56e3c7c66cd8`, affected_ids=[rev_16527d3671ff48d896564a2a420fb60f], and `profile-list` showed profile card `upc_bdc29cff80b34ba69378598f6472497d` with source_ref `src_678b2cfbbbe45f60a130dbd2efe239c1`."
```

### HW-011 Memory Promotion Lifecycle

```yaml
id: HW-011
track: Human Workflow
priority: P1
status: verified
user_value: digest/review 产生的候选能稳定进入 memory/profile，并能随新证据更新 confidence、last_verified_at 和状态。
dependencies:
  - HW-009
  - HW-010
implementation_hint: 明确 memory_candidate/profile_update 的 promotion path：candidate -> review -> apply -> memory/profile card；复用现有 review/audit/store 能力，补 confidence 更新、last_verified_at、source_refs merge 和拒绝/过期状态。自研范围限制在 PSKA 的 canonical lifecycle 和 ACL。
open_source_candidates:
  - Pydantic
  - JSON Schema
  - dateparser
acceptance_gates:
  - apply memory/profile candidate 会创建或更新对应记录，并保留 source_refs 和 audit trail。
  - 重复候选不会无限创建重复 memory/profile；会合并证据或说明跳过原因。
  - memory-list/profile-list 能显示 promoted/updated 状态、confidence 和 last_verified_at。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented memory/profile promotion lifecycle. `MemoryService` now promotes agent memories and profile cards through dedupe-aware create/update helpers, merging source_refs, preserving max confidence, and refreshing last_verified_at. `ReviewService.apply` now promotes `profile_update`, `memory_candidate`, and memory-shaped `low_confidence` reviews through those helpers and records created/updated promotion metadata plus source_refs in review.apply audit events. `memory-list` and `profile-list` now expose promotion_status and last_verified_at. Verification on 2026-06-18: targeted review/CLI/memory/candidate tests `69 passed in 0.25s`; core pytest `202 passed in 8.69s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke on `postgresql:///pska` applied duplicate profile reviews `rev_621e1ffcb9824286a78cc02cb79de97b` and `rev_d029fef09e6e4079a24bbd9009de2abf` into one profile card `upc_46f02ada575942ceb97409f261207e14` with confidence=0.88 and merged source_refs `[src_678b2cfbbbe45f60a130dbd2efe239c1, src_0fe2a7a4b13055c691b40f00824655a0]`; duplicate memory reviews `rev_hw011_memory_a` and `rev_hw011_memory_b` promoted one agent memory `agm_80d2e6b264c84c64b29f3b7adceb24f6` with confidence=0.91, merged source_refs, last_verified_at set, and review.apply audit metadata showing action=created then action=updated with source_refs_merged=1."
```

### HW-012 Retrieval Evaluation Fixtures

```yaml
id: HW-012
track: Human Workflow
priority: P1
status: verified
user_value: 用户能知道 search/agentic-search/GraphRAG 对真实样例问题是否找到了正确出处，而不是只看主观回答质量。
dependencies:
  - HW-003
  - HW-007
implementation_hint: 建立小型 retrieval eval fixture：固定 source/chunk/entity/hyperedge 样例、query、expected citations、expected graph paths 和 gap/conflict expectations；先用 pytest/JSON fixture 做离线回归，再决定是否引入 rerank 或外部 index。
open_source_candidates:
  - pytest
  - rank-bm25
  - sentence-transformers
  - Postgres FTS
acceptance_gates:
  - eval 能报告 lexical/vector/graph context 是否命中 expected citations。
  - graph path relevance 和 evidence coverage 有可读 explanation。
  - 失败时输出 query、missing expected refs 和 diagnostics，而不是只给 pass/fail。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented offline retrieval/GraphRAG eval fixture and `pska_core.cli retrieval-eval`. The JSON fixture contains 4 deterministic cases covering lexical hit, vector/context hit, two-hop graph path relevance with evidence coverage, and conflict expectations. The eval report includes expected/actual/missing refs for citations, lexical/vector sources, graph paths, gaps, and conflicts, plus readable diagnostics such as query, score_debug, result counts, missing expected refs, and graph path explanations. Verification on 2026-06-18: targeted retrieval/CLI tests `3 passed in 0.17s`; core pytest `204 passed in 8.70s`; twitter-x pytest `9 passed in 0.02s`; default fixture smoke `cd core && ../.pska/venvs/pska-py312/bin/python -m pska_core.cli retrieval-eval` returned ok=true with case_count=4, graph path explanation `PSKA -[delegates_to]-> FastReAct -[executes]-> Digest`, evidence_coverage=1.0, and conflict id `graph_conflict:hed_ec6c7a21c3aa5694a136db1097c02e22:contradicts`."
```

### HW-013 Human-readable Ops Briefing

```yaml
id: HW-013
track: Human Workflow
priority: P2
status: verified
user_value: 服务、worker、FastReAct、digest backlog 和 connector 问题能用一条人类可读命令定位，不需要用户直接翻 job log。
dependencies:
  - HW-001
  - HW-004
  - HW-008
implementation_hint: 扩展 daily-status/daily-briefing 或新增 ops-briefing，汇总 worker-level health、failed/stale jobs、digest quality signals、FastReAct readiness、connector state freshness、recommended recovery commands。保持 deterministic fallback；不依赖 LLM。
open_source_candidates:
  - Rich
  - structlog
  - prometheus-client
acceptance_gates:
  - 输出能区分 service down、FastReAct down、stale job、failed digest、connector stale、empty backlog 等状态。
  - 每类问题给出一到两个确定性 recovery command。
  - FastReAct 离线时命令仍能成功输出。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented deterministic `ops-briefing` CLI with JSON and human-readable text output. The briefing aggregates PSKA readiness, worker/job health, stale running jobs, failed digest jobs, digest backlog, FastReAct readiness, connector freshness, diagnostics, and deterministic recovery commands without LLM use. It distinguishes service_down, fastreact_down, stale_job, failed_digest, connector_stale, and empty_backlog states, while returning successfully when FastReAct is offline. Verification on 2026-06-18: targeted CLI/ops tests `3 passed in 0.17s`; core pytest `206 passed in 8.69s`; twitter-x pytest `9 passed in 0.03s`; sample Postgres smoke `cd core && ../.pska/venvs/pska-py312/bin/python -m pska_core.cli --database-url postgresql:///pska ops-briefing --format text --limit 5` returned ok=true and reported service_readiness=ok, fastreact_down with recovery commands, no stale jobs, failed_digest count=5, connector_stale count=1, empty_backlog, and deterministic recommended recovery commands."
```

### HW-014 Local Daemon Productization

```yaml
id: HW-014
track: Human Workflow
priority: P2
status: verified
user_value: 用户能把 local-daemon 当作日常后台服务管理，知道 pid、日志、配置和重启状态，而不是只能前台盯着进程。
dependencies: []
implementation_hint: 在现有 foreground local-daemon 基础上补 status/pid/log path/config check/restart guidance；系统级安装器先做生成 launchd/supervisord 配置或 dry-run，不强制安装。优先薄封装成熟 supervisor，不自研完整进程管理。
open_source_candidates:
  - launchd
  - supervisord
  - honcho
  - foreman
acceptance_gates:
  - local-daemon status 能显示 service/job worker/digest scheduler 子进程状态、pid 和日志路径。
  - config check 能发现缺 DB、端口冲突或 FastReAct URL/token 配置问题。
  - 生成的 supervisor 配置有 dry-run 输出和人工确认路径。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `local-daemon` management actions while preserving foreground `run` as the default. `local-daemon status` now reports each child process name, pid, running/stopped state, pid path, log path, command, and restart guidance. Foreground supervisor runs now write `.pska/run/*.pid` and redirect child stdout/stderr to `.pska/logs/*.log`. `local-daemon config-check` validates PostgreSQL URL shape, detects service port conflicts, and reports FastReAct URL/token diagnostics with deterministic recovery commands. `local-daemon supervisor-config --supervisor supervisord|launchd --dry-run` emits quoted supervisord config or launchd plists plus manual install/status commands without installing anything. Verification on 2026-06-18: targeted local-daemon/CLI tests `6 passed in 0.14s`; core pytest `209 passed in 8.69s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke `local-daemon status --run-dir .pska/run --log-dir .pska/logs` returned three stopped child specs with pid/log paths; `local-daemon config-check` on `postgresql:///pska` detected database_url ok, service_port conflict on 127.0.0.1:8765, FastReAct URL ok with missing token warning and recovery commands; `local-daemon supervisor-config --supervisor supervisord --dry-run` returned ok=true with quoted commands, log paths, and supervisord/supervisorctl guidance."
```

### HW-015 Agent Capture Retention Policy

```yaml
id: HW-015
track: Human Workflow
priority: P2
status: verified
user_value: agentic answer 回流为资料时不会无限重复保存，也能控制敏感内容、保留期限和 review 入口。
dependencies:
  - HW-006
implementation_hint: 为 capture_agent_conversation 增加 retention/dedupe/review policy：基于 purpose、prompt/content hash、source_refs、represented user 和 sensitivity flags 决定保存、跳过或写 review。保留 PSKA-originated answer/citations/trace summary，不保存 FastReAct 私有内部对象。
open_source_candidates:
  - Pydantic
  - JSONL tracing conventions
  - OpenTelemetry
acceptance_gates:
  - 重复 capture 会返回 existing/skipped explanation，不创建无限重复 source items。
  - 高敏感或缺 source_refs 的 capture 可进入 review 或被拒绝保存。
  - daily narrative 和 agentic-search capture 仍复用同一策略。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented agent capture retention/dedupe/review policy in `capture_agent_conversation`. The helper now returns an `AgentCaptureResult` with action/explanation/source_item_id/review_item_id/policy while remaining source-item compatible for existing callers. Captures get a stable dedupe key based on purpose, prompt, answer, represented user, owner, and source_refs; repeated captures return action=existing instead of creating infinite source items. Saved captures store capture_policy and retention metadata with expires_at, preserve PSKA-originated answer/citations/trace_summary, and sanitize tool_calls to avoid persisting private raw FastReAct objects. High/sensitive captures create `sensitive_content` review items; missing source_refs create `low_confidence` review items unless policy rejection is requested. `agentic-search --capture` now reports capture action/explanation/policy, and daily narrative continues to use the same helper. Verification on 2026-06-18: targeted conversation/daily narrative tests `9 passed in 0.08s`; core pytest `212 passed in 8.72s`; twitter-x pytest `9 passed in 0.02s`; sample Postgres smoke on `postgresql:///pska` saved `src_03a6741ab3175bc39da89a72b3337e6a`, repeated the same capture as action=existing with the same source_item_id, and routed a missing-source_refs capture to review `rev_capture_b4213324adb45823a2f94c69233c3c23` with explanation `capture requires source_refs before saving`."
```

## 基线门禁

常规代码改动后默认运行：

```bash
cd core
../.pska/venvs/pska-py312/bin/python -m pytest -q

cd ../channels/twitter-x
../../.pska/venvs/pska-py312/bin/python -m pytest -q
```

Human Workflow smoke 应使用当前 `postgresql:///pska` 样例数据跑通 status/review/jobs/briefing 入口。

## 下一轮任务生成规则

当当前轨道没有 `ready` 任务时，下一轮应先更新本文件，而不是让 agent 猜：

1. 从 [product-design-zh.md](product-design-zh.md) 选一个产品缺口。
2. 用 [architecture-status-zh.md](architecture-status-zh.md) 确认对应模块成熟度和主要缺口。
3. 为新任务写入完整 TODO：id、track、priority、dependencies、open_source_candidates、acceptance_gates、verification_commands。
4. 至少放入一个 `status: ready` 的任务后，agent 才能继续自动实施。
