# PSKA Todo Implement System

日期：2026-06-17

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

## 当前 Ready Backlog

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

## 基线门禁

常规代码改动后默认运行：

```bash
cd core
../.pska/venvs/pska-py312/bin/python -m pytest -q

cd ../channels/twitter-x
../../.pska/venvs/pska-py312/bin/python -m pytest -q
```

Human Workflow smoke 应使用当前 `postgresql:///pska` 样例数据跑通 status/review/jobs/briefing 入口。
