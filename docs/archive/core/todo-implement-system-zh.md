# PSKA Todo Implement System

归档历史任务系统。本文保留旧 Human Workflow/Admin Console backlog 和验收记录，不再是当前计划来源。当前入口见
[中文文档索引](../../README.zh.md)、[Product Design](../../../core/docs/product-design-zh.md)
和 [Architecture Status](../../../core/docs/architecture-status-zh.md)。

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
2. 优先当前产品轨道：后端稳定化优先 `Human Workflow`；产品体验优先 `User Workspace`。`Product UI / Admin Console` 第一轮已完成，除非有明确问题，不继续堆管理页。
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

状态：`HW-001` 到 `HW-008` 已完成并验证。后端稳定化可继续从 `HW-009` 自动选择；产品体验下一轮应从 `APP-001` User Workspace Skeleton 开始。

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

## Product UI / Admin Console Backlog

状态：PSKA Admin Console 第一轮已完成并验证。它证明本地服务、Postgres、service token、HTTP API 和管理入口可用，但它是管理台，不是最终用户的 chat/writer 工作台。后续除非修复明确缺陷，不再把 Product UI 主要迭代用于堆更多管理 API 页面。

### UI-001 Local Web Console Skeleton

```yaml
id: UI-001
track: Product UI
priority: P0
status: verified
user_value: 用户打开一个本地网页就能看到 PSKA 是否可用、今天有什么、下一步该做什么，而不是记一串 CLI 命令。
dependencies:
  - HW-001
  - HW-004
implementation_hint: 在 PSKA HTTP service 上增加本地 Web Console 第一版，入口可为 `/console` 或 `/ui`。第一屏只做 Home Dashboard：复用 daily-status/daily-briefing 的 deterministic 数据，展示 readiness、source/chunk counts、digest backlog、pending reviews、failed jobs、recent sources 和 recommended commands。前端优先使用轻量成熟库或原生静态页面；不引入大型 SPA 复杂度，除非 repo 已有前端框架。
open_source_candidates:
  - HTMX
  - Alpine.js
  - FastAPI/Starlette static patterns
  - Jinja2
  - Tailwind CSS or Pico.css
acceptance_gates:
  - `./scripts/pska serve --port 8765` 后访问本地 console 能看到 Home Dashboard。
  - FastReAct 离线时 console 仍能显示 deterministic 状态和 next actions。
  - 页面不需要用户手动输入 JSON；关键状态和推荐命令可读。
  - 不绕过 PSKA HTTP/API/ACL 边界，不直接访问数据库。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented local `/console` Home Dashboard skeleton on the existing PSKA HTTP service with static HTML/CSS/JS and `/console/data` JSON. The dashboard shows readiness, source/chunk counts, digest backlog, pending reviews, failed jobs, recent sources, deterministic next actions, and recommended commands; it uses the HTTP API/service auth path and does not read the database from browser code. Service-token configurations are supported through an in-page token field instead of manual JSON. Verification on 2026-06-18: targeted HTTP tests `tests/test_fastreact_integration.py` passed (`32 passed`); core pytest `214 passed in 9.71s`; twitter-x pytest `9 passed in 0.02s`; local smoke on port 8876 returned `/console` HTTP 200 with dashboard assets and `/console/data` HTTP 200 with source_counts/recommended_commands/requires_fastreact_online=false when called with the configured service token. FastReAct-offline smoke with `PSKA_FASTREACT_URL=http://127.0.0.1:9` returned `/console/data` HTTP 200, `fastreact_ok=false`, `requires_fastreact_online=false`, and deterministic actions populated. Port 8765 was already occupied by an existing local PSKA service during smoke, so the source-service verification used port 8876."
```

### UI-002 Review Inbox Page

```yaml
id: UI-002
track: Product UI
priority: P0
status: verified
user_value: 用户能在网页里处理 pending review，看到类型、置信度、出处和 approve/reject/apply 操作。
dependencies:
  - UI-001
  - HW-002
  - HW-007
implementation_hint: 增加 `/console/reviews` 或等价页面，复用 `review-list` summary 字段和现有 review approve/reject/apply API。先做单条操作；批量处理依赖 HW-010。高影响 action 和缺 source refs 的 item 必须清楚标记，不自动 apply。
open_source_candidates:
  - HTMX
  - Alpine.js
  - Jinja2
  - JSON Patch
acceptance_gates:
  - 页面列出 pending reviews，并显示 review_type、confidence、source_ref_status、created_at、recommended action。
  - approve/reject/apply 操作调用 PSKA HTTP API，成功后页面状态更新。
  - 不支持 apply 的 review 不展示误导性 apply 按钮。
  - 每个操作仍产生 PSKA audit event。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `/console/reviews` Review Inbox page and `/console/reviews/data` summary endpoint on the existing PSKA HTTP service. The page lists pending review items with review_type, confidence, source_ref_status, created_at, recommended_action, and single-item Approve / Approve + apply / Reject controls. Apply controls are only shown when the review type is supported and required grounding is present; missing source refs and unsupported review types are visibly flagged. Actions call existing PSKA review HTTP APIs (`/review-items/{id}/approve`, `/reject`, `/apply`), preserving the ReviewService audit path. Verification on 2026-06-18: targeted HTTP tests `tests/test_fastreact_integration.py` passed (`34 passed`); core pytest `216 passed in 10.73s`; twitter-x pytest `9 passed in 0.02s`; local smoke on port 8876 returned `/console/reviews` HTTP 200 with page assets and `/console/reviews/data` HTTP 200 with review_items/supports_single_item_actions/total_matching when called with the configured service token. The sample review smoke showed conflict reviews as apply_supported=false/apply_ready=false, so no misleading apply action is exposed."
```

### UI-003 Search And Agentic QA Page

```yaml
id: UI-003
track: Product UI
priority: P1
status: verified
user_value: 用户能在网页里搜索 PSKA，并看到 citations、graph evidence、gaps/conflicts，而不是直接读大段 JSON。
dependencies:
  - UI-001
  - HW-006
  - HW-012
implementation_hint: 增加 `/console/search`，第一版支持 direct search；agentic-search 可作为可选模式，并提供 capture toggle。结果展示 title、snippet、citation、source refs、graph paths 和 diagnostics。LLM/agentic 模式必须明确显示 FastReAct 依赖和失败状态。
open_source_candidates:
  - HTMX
  - Alpine.js
  - Jinja2
  - rank-bm25 existing optional extra
acceptance_gates:
  - 用户输入 query 后能看到 search results 和 citations。
  - agentic/capture 模式成功时能保存 conversation source material；失败时显示可读错误。
  - graph context 和 memory context 不以原始巨大 JSON 倾倒，至少有折叠/摘要。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `/console/search` Search and Agentic QA page plus `/console/search/query` endpoint. Direct mode calls PSKA retrieval and renders results, snippets, citations, graph evidence, memory/profile context, and diagnostics through compact summaries and collapsible sections instead of dumping raw JSON. Agentic mode is explicit in the UI, marks `requires_fastreact_online=true`, and supports a capture toggle; successful capture reuses `capture_agent_conversation` to save the answer/citations/trace as PSKA source material. Verification on 2026-06-18: targeted HTTP tests `tests/test_fastreact_integration.py` passed (`36 passed`), including direct search and fake-agentic capture save; core pytest `218 passed in 11.71s`; twitter-x pytest `9 passed in 0.02s`; local smoke on port 8876 returned `/console/search` HTTP 200 with Agentic/Capture controls and `/console/search/query` HTTP 200 with results/citations/diagnostics/graph_paths for a direct `PSKA` query using the configured service token."
```

### UI-004 Memory/Profile Page

```yaml
id: UI-004
track: Product UI
priority: P1
status: verified
user_value: 用户能检查 PSKA 长期记忆和 profile 当前相信什么、出处是什么、置信度如何。
dependencies:
  - UI-001
  - HW-003
  - HW-011
implementation_hint: 增加 `/console/memory`，展示 agent memories 和 profile cards。第一版只读；后续再做 verify/forget/edit review flow。必须显示 confidence、source_ref_status、last_verified_at、decay_policy 和 created_by_user_id。
open_source_candidates:
  - HTMX
  - Alpine.js
  - Jinja2
acceptance_gates:
  - 页面可列出 memory-list/profile-list 等价信息。
  - 不提供无 audit 的直接修改入口。
  - 缺 source refs 或低 confidence 的内容有视觉标记。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `/console/memory` read-only Memory/Profile page plus `/console/memory/data` endpoint. The page lists agent memories and profile cards with confidence, source_ref_status, last_verified_at, decay_policy, created_by_user_id, status/promotion status, and visible attention markers for missing source refs, low confidence, or forgotten memories. No mutation controls are exposed; the endpoint returns `read_only=true` and only calls store list methods. Verification on 2026-06-18: targeted HTTP tests `tests/test_fastreact_integration.py` passed (`37 passed`); core pytest `219 passed in 11.74s`; twitter-x pytest `9 passed in 0.02s`; local smoke on port 8876 returned `/console/memory` HTTP 200 with Agent Memories/Profile Cards assets and `/console/memory/data` HTTP 200 with agent_memories/profile_cards/read_only/source_ref_status using the configured service token."
```

### UI-005 Jobs And Daemon Ops Page

```yaml
id: UI-005
track: Product UI
priority: P1
status: verified
user_value: 用户能在网页里看到 PSKA service、local worker、FastReAct digest worker、digest backlog 和失败任务状态。
dependencies:
  - UI-001
  - HW-013
  - HW-014
implementation_hint: 增加 `/console/jobs` 或 `/console/ops`，展示 jobs stats、queued/running/failed、digest backlog、stale jobs、FastReAct readiness 和 recovery commands。注意 PSKA local worker 不应消费 `digest_via_fastreact`；页面应提示 digest backlog 应由 FastReAct worker 处理。
open_source_candidates:
  - HTMX
  - Alpine.js
  - Jinja2
  - structlog
acceptance_gates:
  - 页面能显示 job counts、digest backlog、recent failures 和 running/stale job 状态。
  - 对 FastReAct timeout、service down、端口占用、本地 worker 抢 digest job 这类问题给出明确 recovery command。
  - 不自动执行 destructive job 操作；retry/cancel 需要显式确认。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `/console/jobs` read-only Jobs/Ops page plus `/console/jobs/data` endpoint. The page shows service/FastReAct readiness, job counts by status/type, digest backlog, recent failed jobs, running/stale jobs, ops issues, and recovery commands. It explicitly notes that digest_via_fastreact backlog belongs to the FastReAct digest worker, includes commands for FastReAct down, stale job recovery, failed digest inspection, empty backlog scheduling, and port 8765 old-daemon checks, and exposes no retry/cancel/recover buttons. Verification on 2026-06-18: targeted HTTP tests `tests/test_fastreact_integration.py` passed (`38 passed`); core pytest `220 passed in 12.76s`; twitter-x pytest `9 passed in 0.02s`; local smoke on port 8876 returned `/console/jobs` HTTP 200 with Recovery Commands/Recent Failures assets and `/console/jobs/data` HTTP 200 with worker_health/digest_backlog/recommended_recovery_commands/read_only using the configured service token."
```

### UI-006 Sources And Connector Page

```yaml
id: UI-006
track: Product UI
priority: P2
status: verified
user_value: 用户能知道 PSKA 当前接入了哪些资料源、最近导入了什么、files connector 是否正常。
dependencies:
  - UI-001
implementation_hint: 增加 `/console/sources`，展示 recent sources、source_channel 分布、connector states、files root/sync status 和 sync commands。第一版不扩新 connector，只管理 Twitter/X archive 和 local files/notes root。
open_source_candidates:
  - HTMX
  - Alpine.js
  - Jinja2
  - watchdog existing optional extra
acceptance_gates:
  - 页面展示 source counts、recent sources、connector state 和 files root 配置。
  - 提供 files-sync/files-watch 的推荐命令或安全触发入口。
  - 不引入新的 connector scope。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `/console/sources` read-only Sources/Connector page plus `/console/sources/data` endpoint. The page shows source counts, channel distribution, recent sources, connector states, files roots from connector permission_scope/config, and recommended `files-sync`/`files-watch`/connector-state commands. It does not mutate connector state or add new connector scope; new roots still require explicit files-sync/files-scan authorization. Verification on 2026-06-18: targeted HTTP tests `tests/test_fastreact_integration.py` passed (`39 passed`); core pytest `221 passed in 13.21s`; twitter-x pytest `9 passed in 0.02s`; local smoke on port 8876 returned `/console/sources` HTTP 200 with Source Channels/Connector States/Files Commands assets and `/console/sources/data` HTTP 200 with source_counts/source_channels/connector_state/recommended_commands/read_only using the configured service token."
```

## User Workspace Backlog

状态：基础设施体检通过，可以进入下一轮产品迭代。2026-06-18 验证：core pytest `222 passed in 13.74s`，twitter-x pytest `9 passed in 0.02s`，受影响模块测试 `64 passed in 12.86s`，临时 HTTP smoke 在 `8877` 验证 `/health`、`/console`、service-token-protected `/console/data` 均按预期工作。

### APP-001 User Workspace Skeleton

```yaml
id: APP-001
track: User Workspace
priority: P0
status: verified
user_value: 用户打开一个本地页面后，看到的不是 API 管理台，而是可以开始对话、查看资料和进入写作的主工作区。
dependencies:
  - UI-001
  - UI-003
implementation_hint: 在现有 PSKA HTTP service 上新增 `/workspace` 或 `/app`。第一版不引入大型 SPA，优先复用当前静态页面模式和 `/console/search/query` 能力，但界面信息架构必须以用户流程组织：左侧为 corpus/source 入口，中间为 chat，右侧为 evidence/context inspector，顶部可进入 writer。保留 service token 输入和 sessionStorage，不绕过 HTTP/API/ACL 边界。
open_source_candidates:
  - HTMX
  - Alpine.js
  - Jinja2
  - Tiptap/ProseMirror later for writer mode
acceptance_gates:
  - `/workspace` 或 `/app` 能在本地 service 下打开，并清楚区分 Chat、Corpus、Writer、Evidence 区域。
  - Chat 区域能调用现有 direct search/agentic search endpoint，默认中文展示回答状态、citations、graph evidence、gaps/conflicts。
  - Evidence 区域不倾倒原始巨大 JSON，至少展示 source refs、citation snippets、graph paths、memory/profile context 摘要。
  - FastReAct 离线时，workspace 仍能做 direct retrieval，并明确提示 agentic 能力不可用。
  - 不新增直接 DB 访问；所有数据通过 PSKA HTTP/API/ACL。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `/workspace` and `/app` User Workspace skeleton on the existing PSKA HTTP service with static HTML/CSS/JS plus `/workspace/search/query`. The first screen is organized around user workflow rather than admin operations: left corpus/source rail, center chat composer/results, right evidence/context inspector, and a writer draft band. The workspace keeps the service-token/sessionStorage pattern, calls PSKA HTTP APIs only, and reuses existing direct/agentic search behavior through a thin workspace summary wrapper. Evidence is summarized into citations/source refs, citation snippets, graph paths, memory/profile context, gaps, and conflicts instead of dumping raw JSON. Agentic failures return a clear unavailable status with direct retrieval fallback evidence. Verification on 2026-06-18: targeted HTTP integration tests for workspace and console search passed (`4 passed, 39 deselected in 2.17s`); full `tests/test_fastreact_integration.py` passed (`43 passed in 14.33s`); core pytest passed (`225 passed in 15.28s`); twitter-x pytest passed (`9 passed in 0.02s`)."
```

### APP-002 Corpus / Wiki Explorer

```yaml
id: APP-002
track: User Workspace
priority: P0
status: verified
user_value: 用户能看懂 PSKA 当前到底保存了哪些 source、document、chunk、citation、entity、hyperedge、memory 和 profile，而不是只看到统计数字或 ID。
dependencies:
  - APP-001
  - UI-006
  - UI-004
implementation_hint: 为 workspace 增加 corpus explorer 页面或面板，复用 store/list/search 能力，必要时新增只读 summary endpoint。优先展示 source item、chunk snippet、source refs、channel、created_at、entity mentions、graph edge evidence 和 memory/profile 内容摘要。第一版只读。
open_source_candidates:
  - TanStack Table
  - HTMX
  - Alpine.js
acceptance_gates:
  - 用户能按 source channel、时间、文本关键词查看 source/chunk 内容摘要。
  - memory/profile 不再只展示 ID，必须展示内容、confidence、source_ref_status 和出处。
  - graph/hyperedge 至少能看到 subject、predicate、object、confidence、evidence citations。
  - 页面不提供无 audit 的直接修改入口。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented APP-002 Corpus / Wiki Explorer inside the existing `/workspace` surface plus `/workspace/corpus/data`. The endpoint is read-only, service-token protected with the existing HTTP context path, and summarizes source items, chunks, documents, entities, hyperedges, agent memories, and profile cards for the represented/current owner. The workspace corpus rail now has source-channel, text query, and limit filters, renders source/chunk snippets, shows memory/profile content with confidence/source_ref_status, and displays hyperedge relation_type, members, confidence, evidence_text, and source_refs without exposing mutation controls. Added `.headroom/` to `.gitignore` per local setup. Verification on 2026-06-18: targeted workspace tests passed (`4 passed, 40 deselected in 2.15s`); full `tests/test_fastreact_integration.py` passed (`44 passed in 14.84s`); core pytest passed (`226 passed in 15.79s`); twitter-x pytest passed (`9 passed in 0.02s`)."
```

### APP-003 Writer Mode V0

```yaml
id: APP-003
track: User Workspace
priority: P1
status: verified
user_value: 用户能在富文本里写作，圈选文本后让 PSKA 基于个人资料、记忆、profile、citations 和 graph evidence 给中文写作建议。
dependencies:
  - APP-001
  - APP-002
implementation_hint: 先做最小可用 writer：一个持久化前可只保存在浏览器 session 的编辑区、选中文本、query builder、建议面板和 evidence inspector。后续再引入 Tiptap/ProseMirror 持久文档模型。
open_source_candidates:
  - Tiptap
  - ProseMirror
  - CodeMirror
acceptance_gates:
  - 能选中文本并生成带 query/context 的建议请求。
  - 建议默认中文，并展示 citations、gaps/conflicts 和使用了哪些 memory/profile。
  - 不把未确认建议自动写入 memory/profile/graph。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented APP-003 Writer Mode V0 inside `/workspace`. The writer now uses a contenteditable draft editor persisted in browser sessionStorage, captures selected text into a query builder, calls the read-only `/workspace/writer/suggest` endpoint, and renders default Chinese suggestions plus writer evidence. The endpoint builds a PSKA retrieval query from selected text/draft/instruction, returns Chinese deterministic writing guidance, citations/source refs, graph paths, gaps/conflicts, and memory/profile usage counts, and explicitly does not mutate memory, profile, or graph state. Verification on 2026-06-19: targeted workspace tests passed (`5 passed, 40 deselected in 2.70s`); full `tests/test_fastreact_integration.py` passed (`45 passed in 14.85s`); core pytest passed (`227 passed in 16.30s`); twitter-x pytest passed (`9 passed in 0.02s`)."
```

### APP-004 Writer Ambient Evidence Suggestions

```yaml
id: APP-004
track: User Workspace
priority: P0
status: ready
user_value: 用户写作时不需要每次手动选中文本发起查询，PSKA 能低打扰地扫描草稿，发现可以补充引用、证据、memory/profile 约束或图谱关系的位置。
dependencies:
  - APP-003
  - RAG-004
implementation_hint: 在 Writer Mode V0 的 selected-text 查询基础上增加 ambient scan。前端按段落或当前光标附近文本生成候选 spans，后端新增只读 `/workspace/writer/scan` 或复用 suggest endpoint 的 scan mode。扫描必须节流、可关闭、只返回建议和 evidence，不自动改写正文，不自动写入 memory/profile/graph。建议类型至少包括 citation_needed、graph_context_available、memory_profile_constraint、possible_conflict、gap_to_fill。第一版可以 deterministic：基于段落关键词、retrieval top result、graph paths、memory/profile context 和 gaps/conflicts 生成建议。
open_source_candidates:
  - Tiptap/ProseMirror decoration model later
  - DOM Selection / Range APIs for current contenteditable implementation
  - debounce/throttle utility
acceptance_gates:
  - 用户在 writer 中输入或停顿后，系统能对当前草稿生成 evidence suggestions，不要求手动 selected text。
  - 每条建议绑定 draft span 或 paragraph id，并展示建议类型、短说明、citations/source refs、graph paths 或 memory/profile 使用说明。
  - 用户能接受、忽略或重新查询建议；接受建议不直接写长期 memory/profile/graph。
  - FastReAct/LLM 离线时仍能用 direct retrieval/GraphRAG deterministic fallback 给出建议或明确无建议。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q tests/test_fastreact_integration.py
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: ""
```

### RAG-001 Retrieval Quality Replay

```yaml
id: RAG-001
track: Retrieval Quality
priority: P0
status: verified
user_value: 用户能知道 PSKA 的 agentic search/graph-assisted retrieval 在真实问题上是不是找到了正确资料，而不是只看一个看似流畅的回答。
dependencies:
  - HW-012
implementation_hint: 扩展现有 retrieval-eval fixture，加入从当前样例库和真实使用问题抽出的中文 query、expected source refs、expected citation snippets、expected graph paths 和 known gaps。报告应区分 lexical/vector/graph/memory/profile 命中与缺失。
open_source_candidates:
  - rank-bm25
  - rapidfuzz
  - Postgres FTS later
  - HippoRAG/PPR evaluation later
acceptance_gates:
  - 至少加入 5 个中文真实问题 fixture。
  - 报告能列出 expected/actual/missing citations 和 graph paths。
  - 失败案例不被吞掉，能进入下一轮检索改进。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q tests/test_retrieval_eval.py
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pska_core.cli retrieval-eval
  - cd core && PSKA_LLM_API_KEY_FILE="$HOME/api_key.txt" SSL_CERT_FILE="$(../.pska/venvs/pska-py312/bin/python -c 'import certifi; print(certifi.where())')" ../.pska/venvs/pska-py312/bin/python -m pska_core.cli retrieval-eval --real
commit_evidence: "Implemented RAG-001 retrieval quality replay expansion. The eval fixture now has 9 cases including 5 Chinese real-use questions covering workspace components, writer/evidence graph paths, memory preference, profile writing preference, and real RAG gate behavior. Reports now include expected/actual/missing citations, lexical hits, vector hits, graph paths, gaps/conflicts, memory IDs, profile card IDs, and agentic LLM replay diagnostics. Added `retrieval-eval --real`, which forces real BGE-M3 embeddings even when default config sets embedding disabled, backfills fixture chunk embeddings with the actual provider, and runs LLM-backed AgenticSearchService for every case instead of fake/stub LLM behavior. Verification on 2026-06-19: targeted `tests/test_retrieval_eval.py` passed (`2 passed in 0.04s`); offline CLI `retrieval-eval` returned ok=true with case_count=9, real=false, provider=fixture-embeddings, and at least 5 zh_* cases; real CLI gate with `PSKA_LLM_API_KEY_FILE=$HOME/api_key.txt` and `SSL_CERT_FILE=certifi.where()` returned ok=true, real=true, embedding provider=bge-m3 model=BAAI/bge-m3 dimensions=1024 backfill embedded=9 failed=0, LLM model=deepseek-v4-flash base_url=https://api.deepseek.com, and agentic_cases=9. Core pytest passed (`227 passed in 16.31s`); twitter-x pytest passed (`9 passed in 0.02s`)."
```

### RAG-002 PPR-style Graph-aware Retrieval

```yaml
id: RAG-002
track: Retrieval Quality
priority: P0
status: verified
user_value: 用户的个人资料、聊天 digest、memory/profile 和实体关系图能实质影响检索，而不是只在答案旁边展示 graph evidence；最差情况下仍退回普通 RAG。
dependencies:
  - RAG-001
  - HW-012
implementation_hint: 不做 GNN。参考官方 HippoRAG 2 `OSU-NLP-Group/HippoRAG` 的在线检索主链：query_to_fact/fact rerank 选择相关 facts，由 fact 中 subject/object 实体给 graph reset probability 注入主权重，同时 dense passage 以小权重作为 passage seed，最后 PPR 输出 passage/chunk 分数。PSKA 侧把 canonical hyperedge 作为 fact/triple 层，把 source refs/chunks 作为 passage evidence 层，并保持 ACL、citations、review/audit 边界。无 query-relevant fact 或 query entity seed 时明确退回普通 RAG。
open_source_candidates:
  - NetworkX later
  - igraph later
  - HippoRAG 2 design reference
  - Postgres recursive CTE later
acceptance_gates:
  - graph-connected evidence chunk 即使没有直接词汇命中，也能通过 PPR expansion 进入 retrieval results。
  - 每个新增候选仍保留 citation/source_item_id/chunk_id，不绕过 ACL。
  - response.score_debug 能区分 `ppr_chunk_entity_fusion` 和 `rag_fallback`，并展示 PPR node/edge/seed/expanded candidate 统计。
  - 没有可用图信号时 search 仍作为普通 lexical/vector RAG 正常工作。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q tests/test_memory_hypergraph_agentic.py
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented request-scoped PPR-style graph-aware retrieval in `RetrievalService`, then aligned it more closely with the official HippoRAG 2 code from `OSU-NLP-Group/HippoRAG` (MIT, cloned under `workspaces/references/HippoRAG` for reference). The ranker now treats PSKA hyperedges as the fact/triple layer, scores query-relevant facts, uses top fact member entities as primary PPR seeds, uses dense/lexical passage hits only as low-weight passage priors, runs lightweight personalized PageRank over an ACL-visible chunk/entity/evidence graph, fuses `graph_ppr` scores into chunk ranking, and can expand graph-connected evidence chunks into retrieval results while preserving citations. Dense passage seeds alone no longer trigger graph rerank; no relevant facts/entities returns explicit `graph_ranker=rag_fallback`. Verification on 2026-06-19: targeted `tests/test_memory_hypergraph_agentic.py` passed with new coverage for graph-connected chunk expansion and no-graph fallback; affected retrieval/workspace/FastReAct tests passed (`68 passed in 15.39s`); full core pytest passed (`229 passed in 16.31s`); twitter-x pytest passed (`9 passed in 0.02s`)."
```

### RAG-003 HippoRAG Offline/Online Split

```yaml
id: RAG-003
track: Retrieval Quality
priority: P0
status: verified
user_value: PSKA 不再只在在线 search 时临时拼 graph，而是明确拥有 HippoRAG-style 离线图索引层，并在在线检索时复用该索引注入 query-specific seeds。
dependencies:
  - RAG-002
implementation_hint: 参考 HippoRAG 2 的 offline indexing / online retrieval 分层。PSKA 离线侧从 canonical source/chunk/entity/hyperedge/source_refs 构建 fact/entity/passage 异构图索引对象，包含 fact nodes、phrase/entity nodes、passage/chunk nodes、fact-to-entity、fact-to-passage、entity-to-passage 和 entity-to-entity 边。在线侧只做 query fact scoring、query entity linking、dense/lexical passage prior、PPR 和 result fusion。第一版先做请求级 rebuildable index；后续再持久化到 Postgres 表或 graph cache。
open_source_candidates:
  - HippoRAG 2 official code
  - igraph
  - NetworkX
  - Postgres materialized view / recursive CTE
acceptance_gates:
  - 独立 `HippoRAGOfflineIndex` 能从现有 PSKA canonical graph 构建 fact/entity/passage 图，并报告 graph_info。
  - retrieval 在线阶段使用 offline index，而不是手写散落构图逻辑。
  - score_debug 暴露 `hipporag_offline_graph`，让产品/评测可见离线图规模。
  - 现有 RAG replay、workspace/search 和 ACL 相关测试继续通过。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q tests/test_memory_hypergraph_agentic.py tests/test_retrieval_eval.py tests/test_fastreact_integration.py
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
  - cd channels/twitter-x && ../../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented `pska_core.hipporag_index.HippoRAGOfflineIndex` as a PSKA-native offline graph index inspired by HippoRAG 2. The index builds fact nodes from canonical hyperedges, entity/phrase nodes from PSKA entities, passage nodes from chunks, fact-to-entity edges, fact-to-passage evidence edges from source_refs, entity-to-passage mention/evidence edges, and entity-to-entity relation edges. `RetrievalService` now builds this ACL-visible offline index and uses it for online query fact scoring, query entity seeds, low-weight passage priors, PPR, and chunk result fusion. `score_debug.hipporag_offline_graph` exposes graph stats. Verification on 2026-06-19: affected retrieval/workspace/FastReAct tests passed (`69 passed in 14.39s`); full core pytest passed (`230 passed in 16.34s`); twitter-x pytest passed (`9 passed in 0.02s`)."
```

### RAG-004 Fact / Entity Embedding Linking

```yaml
id: RAG-004
track: Retrieval Quality
priority: P0
status: verified
user_value: HippoRAG 的 fact retrieval 和 entity linking 不再只靠词面匹配，能用 embedding-style similarity 找到语义相关 facts/entities，并继续保持普通 RAG fallback。
dependencies:
  - RAG-003
implementation_hint: 参考 HippoRAG 2 的 `query_to_fact`、entity embedding store 和 fact rerank 设计。PSKA 第一版不引入重依赖，而是在 `HippoRAGOfflineIndex` 中加入 fact/entity embedding slots、`with_embeddings(provider)`、query embedding scoring、entity linking 和 `HippoRAGFactReranker` hook。RetrievalService 在有 embedding provider 时为 fact/entity/linking 使用 embedding score，provider 失败则保留 lexical fallback。fact embedding 只嵌入实体+关系结构文本，避免 evidence 泛词把无关事实点亮；embedding-only 候选有强信号和泛化门控；graph expansion 的 boost 与 RRF 同量级，避免覆盖直接 lexical/vector 证据。
open_source_candidates:
  - HippoRAG 2 official code
  - BGE-M3 existing provider
  - cross-encoder reranker later
  - FastReAct/LLM reranker later
acceptance_gates:
  - offline index 能用 embedding score 找到 lexical 无命中的 fact。
  - offline index 能用 embedding score link entity。
  - RetrievalService 的 score_debug 暴露 `hipporag_embedding_linking`。
  - embedding provider 缺失或失败不破坏 lexical/vector RAG fallback。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q tests/test_memory_hypergraph_agentic.py
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q tests/test_memory_hypergraph_agentic.py tests/test_retrieval_eval.py tests/test_fastreact_integration.py
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented fact/entity embedding-style linking in `HippoRAGOfflineIndex`. The index now supports `with_embeddings(provider)`, stores fact/entity vectors, scores facts with merged lexical + embedding relevance, links entities with lexical + embedding relevance, and exposes a reachable `HippoRAGFactReranker` hook for future LLM/cross-encoder reranking. `RetrievalService` now invokes embedding-backed fact/entity linking when an embedding provider is configured and exposes `score_debug.hipporag_embedding_linking`. The implementation embeds fact structure as entity labels + relation type, applies strong-similarity and ambiguous embedding-only candidate gates, and keeps graph expansion boost at RRF-compatible scale so direct lexical/vector evidence is not displaced. Verification on 2026-06-19: affected retrieval/eval/FastReAct tests passed (`71 passed in 15.42s`); full core pytest passed (`232 passed in 16.35s`). The current workspace does not contain a twitter-x connector path, so that historical gate was not rerun in this checkout."
```

### RAG-005 Incremental Offline Index Pipeline

```yaml
id: RAG-005
track: Retrieval Quality
priority: P0
status: verified
user_value: 用户可以默认把当前 cwd 下的授权文档持续纳入 embedding 和图谱化处理；新增、修改、删除资料会增量更新，而不是每次在线查询临时重建全图。
dependencies:
  - RAG-003
  - RAG-004
implementation_hint: 把 HippoRAG-style offline stage 产品化为 PSKA 的增量索引管线。为 source/chunk/entity/hyperedge/fact embedding/index state 增加 content_hash、mtime、visibility_version、embedding_model、index_version 和 last_indexed_at 语义；ingest 或 file sync 后只处理 dirty source/chunk；权限变化要让 index cache 失效或按 ACL 过滤；删除 source 要 tombstone/清理 chunk embeddings、fact-to-passage links 和 graph cache。第一版可以先持久化 index state 和 dirty queue，online retrieval 仍可在请求内构建 ACL-visible subgraph；后续再把 fact/entity/passage adjacency、fact/entity embeddings 和 PPR-ready graph cache 持久化。
open_source_candidates:
  - watchdog/files connector existing state
  - pgvector
  - Postgres materialized views or cache tables
  - HippoRAG 2 offline indexing reference
acceptance_gates:
  - 当前 cwd 或授权 notes root 的新增/修改文档能被标记为 dirty，并只重嵌入/重抽取受影响 chunk。
  - source 删除、移动、visibility/owner 变化会让相关 chunk/entity/fact index state 失效，不产生越权检索。
  - `score_debug` 或 ops endpoint 能报告 offline index freshness、dirty counts、last_indexed_at、embedding_model/index_version。
  - online GraphRAG 可以复用 index state；index 不可用或过期时仍能 fallback 到普通 RAG 或请求级 rebuild。
verification_commands:
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q tests/test_memory_hypergraph_agentic.py tests/test_retrieval_eval.py tests/test_fastreact_integration.py
  - cd core && ../.pska/venvs/pska-py312/bin/python -m pytest -q
commit_evidence: "Implemented the first incremental offline index pipeline for the HippoRAG-style offline stage. Added durable `offline_index_states` schema, `OfflineIndexState` model, store APIs for dirty/indexed/tombstoned states, and `OfflineIndexService` to mark ingested source/chunk objects dirty, process only dirty chunk embeddings, tombstone source index state, and report freshness. Ingest now marks new source/chunks dirty with content hash, visibility version, embedding model, index version, and dirty reason; visibility changes invalidate affected index state; retrieval score_debug and service readiness/metrics expose offline_index_freshness/dirty counts/last_indexed_at while preserving request-scoped ACL-visible graph rebuild fallback. Verification on 2026-06-19: RAG-005 focused tests `88 passed in 15.45s`; full core pytest `241 passed in 16.37s`; twitter-x pytest `9 passed in 0.02s`."
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

1. 从 [product-design-zh.md](../../../core/docs/product-design-zh.md) 选一个产品缺口。
2. 用 [architecture-status-zh.md](../../../core/docs/architecture-status-zh.md) 确认对应模块成熟度和主要缺口。
3. 为新任务写入完整 TODO：id、track、priority、dependencies、open_source_candidates、acceptance_gates、verification_commands。
4. 至少放入一个 `status: ready` 的任务后，agent 才能继续自动实施。
