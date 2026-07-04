# 里程碑：Phase 2 搜读问一体化

本里程碑承接 Phase 1 多知识库 RAG 底座。目标是把 Ask 的引用从“可见 citation”升级为“可检查、可回到原文、可基于原文继续追问”的读问闭环。

## 范围

本阶段必须完成：

- `CitationInspector`：Ask 结果中的引用可以展开查看 KB 归属、source/document/chunk/passage 坐标、score、source window 文本。
- `ReaderPane`：用户可以从引用进入更完整的原文阅读上下文。
- source window -> 原文回跳：如果引用有 URL 或原文定位，前端提供稳定入口。
- 从原文追问：用户能基于当前 citation/source window 发起下一问。
- `ProcessingTimeline`：Ask 的理解、检索、读取、生成、evidence check 过程可解释。
- no-answer diagnostics 产品化：没有答案时展示原因，而不是只显示空结果。

本阶段不做：

- ResearchTask 表和异步 deep task queue。
- 完整 team/shared/RBAC。
- per-KB parser/embedding 自动 backfill。
- Graph 大规模交互重构。

## 当前进度

截至当前实现，Phase 2 已开始落地第一条产品切片：

- Ask 结果中的 `EvidenceWindow` 已升级为 citation inspector，展示 citation 坐标、KB 归属、source window policy、score、原文字符范围、URL 和原文窗口。
- 新增 `GET /workspace/reader/source`，按 `source_item_id` 返回当前用户可见且 KB scope 覆盖的 source、documents、chunks、passage windows 和 `scope_applied`。
- Citation inspector 增加“查看原文”动作，拉取 reader source 后在同一侧栏展开 ReaderPane，展示原文正文和按 ordinal 排列的 chunks。
- ReaderPane 已把 citation 的 `source_window.start_char/end_char` 或 source window 文本映射回原文正文与 chunk，打开原文时会围绕引用位置截取上下文并高亮命中段落。
- Citation inspector 增加“追问这段”动作。
- Today/Ask composer 会把选中的 citation 转成下一问草稿，并显示可清除的原文焦点 chip。
- 下一问会把该 citation 的 `source_item_id` 注入 Ask `scope.source_item_ids`，避免从原文继续问时范围回退到整个 KB。
- ReaderPane 已支持选中原文后“追问选区”：选区文本会被包装为同一套 evidence ref，保留 source/document/chunk/passage 与 KB lineage，并继续注入 `source_item_ids` hard scope。
- Today Ask 结果已增加“生成 Brief”动作：有 `run_id` 且带可检查引用的 Ask run 可以直接生成 Evidence Brief / Writing board，保留 Ask run lineage、citations/source_refs 和 KB lineage，成功后进入 Writing。
- Writing 入口页已增加 Evidence Brief Library：按真实 `metadata.kind=evidence_wiki_brief` 的 Writing board 列出 Brief，详情展示 lineage、KB scope、review 状态和 source refs，并可通过现有 Writing/Evidence Brief API 执行重新生成、过期、回滚与恢复。
- Citation inspector 已复用到 Writing answer/evidence/draft 节点和 Graph 节点 / Graph Path citations；这些入口共享同一套 citation 坐标、KB lineage、ReaderPane 原文加载和高亮能力。
- no-answer diagnostics 已从维度列表升级为“原因 + 建议下一步”的行动面板，按通用状态提示扩大范围、检查索引、补证据、重试 Deep Ask/MCP 或检查权限/策略。
- `ProcessingTimeline` 已从原始事件日志升级为五阶段摘要，合并 `progress`、`agent_steps`、agentic trace 和 `quality_signals`，展示理解、检索、读取、生成、证据校验的状态、证据数、引用数和质量状态；原始过程仍保留为可展开明细。
- Writing 节点和 Composer 的 Ask timeline 已复用同一套阶段摘要，运行中的 preview 与持久化的 `last_ask` 都会携带 `progress`、`evidence_check` 和 `quality_signals`。
- Writing answer/evidence/draft 节点顶部已增加 Ask 健康指示，把 `quality_signals`、`evidence_check`、节点状态和引用数提炼成“有引用 / 证据不足 / 需复核 / 失败 / 无需证据”等短标签，展开后仍可查看完整阶段和原文引用。
- Review Center 列表已复用同一类证据健康摘要，把 `source_ref_status`、`quality_tier`、`review_eligible` 和 apply readiness 提炼成“可审核 / 缺证据 / 仅诊断 / 需检查”等短标签。
- Review Center 卡片已增加“证据对比”折叠区，复用 citation inspector / ReaderPane 展示每条 Review source ref 的 KB 归属、source/document/chunk/passage 坐标、预览文本和原文读取能力。
- Review Center 已增加批量选择工具条：pending 列表可批量批准/拒绝，approved 列表可批量应用；批量动作仍逐条调用真实 Review API，并在每张卡片上保留处理状态。
- Review Center 已增加“候选对照”面板：选择两条以上 Review candidate 时，主区会基于真实候选数据横向比较状态、建议、证据健康、KB lineage、置信度、引用数、source 数和共享 source refs。
- Review Center 已增加“稍后”队列：pending Review 可以单条或批量 snooze 到 `snoozed` 状态，`snoozed` tab 可以单条或批量恢复到 pending；后端 `ReviewService` 会写入 `review.snooze` / `review.restore` audit event。
- Review Center 的 applied 卡片已增加“应用 lineage”面板，基于真实 `application_result` 展示写入目标、审计动作、状态、target id 和应用时保留的证据引用数量。
- Today 右侧 Review 卡片已接入同一类证据健康摘要，把待审候选在日常入口中标成“需复核 / 补引用 / 缺证据”，减少用户必须先进入 Review Center 才知道证据状态的跳转。
- Ask scope selector 的多知识库菜单已支持搜索 KB，并在每个候选项上展示可检索状态、资料数、chunk 数和 embedding 覆盖率，便于发问前确认范围 readiness。
- Knowledge Base 详情页已按产品心智收敛为 `资料 / Ask / 处理 / Digest / Graph / Writing / 设置` tabs：复用真实导入、文档 lifecycle、证据搜索、chunk preview、高级同步、Digest 日志、当前 KB Graph/Memory 摘要、当前 KB Writing boards 和 Prompt Profile/KB 管理入口，避免资料库工作台继续平铺成一长页。
- Graph Ask 主路径和 legacy Graph Path 面板都已增加证据健康摘要，把 citations、agentic repair 和错误状态提炼成“有引用 / 缺引用 / 已重写 / 需复核 / 失败”等短标签。
- Linking digest 生成的 `relationship_candidate` Review proposal 已携带 `relation_type`、成员、证据摘要和 source refs，Review Center 的 apply-ready 判断会校验真实 apply 所需字段，避免“看起来可应用但应用失败”的候选。
- Review Center 的 applied relationship item 已持久返回 `application_result.target_ids.created_hyperedge_id`，并提供“在 Graph 查看”动作，点击后会切到 Graph、拉取对应 `hyperedge:<id>` 的 1-hop subgraph 并打开节点 inspector。
- Graph Ask 已增加“保存到 Writing”动作：即时 Graph Ask 没有持久化 `ask_run_id` 时，也会用真实 Writing API 创建 Graph Brief board、answer/evidence 节点和 citation edge，并自动打开刚生成的 board。
- Graph 节点详情已增加“保存证据”动作：选中的 source/document/passage/claim 等带 `source_refs` 节点可以直接创建 Graph Node Writing board、evidence 节点和章节引用边，保留 graph node metadata 与 citation lineage。
- Evidence Wiki page preview 已增加页级内容编辑、内容修订恢复、内容发布同步状态和 durable taxonomy：页面标题/摘要/正文保存到 Writing board 与 managed `evidence_wiki_page_body` 节点，每次保存会留下 `wiki_content_revisions` 快照并可恢复旧修订；已发布页面被编辑后会标成“待更新发布”，再次 publish 会把当前 revision 记录为已发布内容；tags/categories/topics/collections 保存到 board metadata，Wiki search 可按 taxonomy facet 过滤，相关页面会纳入共享 taxonomy。
- `frontend/e2e/multi-kb-scoped-ask.spec.ts` 已扩展真实浏览器验收：Knowledge Base 详情 tabs 可切换到 `资料 / Ask / 处理 / Digest / Graph / Writing / 设置` 并显示对应真实入口；UI Ask 展示 alpha citation inspector，点击“查看原文”后 ReaderPane 包含 alpha 原文且不包含 beta secret；在 ReaderPane 选中 alpha 高亮原文并点击“追问选区”后，输入框包含 alpha `source_item_id` 和选区文本；点击“生成 Brief”后会进入 Writing，最新 brief 节点包含 alpha 证据且不包含 beta secret；返回 Evidence Brief Library 后会验证该 Brief 出现在列表/详情中，保存两版 Wiki 页级正文、看到“待更新发布”、从 revision list 恢复第一版并点击“更新发布”回到“已同步发布”，保存 taxonomy 并用 facet 过滤，执行过期、恢复、回滚、恢复，并通过 lineage 重新生成 Brief，确认 alpha 证据仍不泄露 beta；Graph Ask 在空图/有图主路径也会显示 `graph-path-evidence-health` 且不泄露 beta 证据，点击“保存到 Writing”后进入 Graph Brief board 且 answer/evidence 节点保留 alpha citation；Graph 本地搜索选中 alpha 节点后，点击“保存证据”会进入 Graph Node board 且 evidence 节点保留 alpha 原文、不包含 beta secret；真实 linking digest 生成的 Review candidate 会在 Today 右栏显示 `today-review-evidence-health`，在 Review Center 显示 `review-evidence-health`，并能打开“证据对比”后从 Review citation inspector 进入 ReaderPane 检查原文；一个真实 Review candidate 会被 snooze 到 `snoozed` tab 并恢复回 pending；额外两个真实 Review candidates 会被批量选择，出现“候选对照”横向比较面板后再批量拒绝；主 Review candidate 点击“批准并应用”后会进入已应用列表、显示“应用 lineage”里的 Graph relationship target id 和证据数量、创建 graph relationship，并能从 applied card 打开 Graph inspector 查看 `shared_topic` hyperedge。
- `frontend/e2e/writing-workspace.spec.ts` 已扩展真实浏览器验收：Writing answer/question 运行后应出现 `writing-node-ask-health`，并且持久化节点/`last_ask` 保留可支撑健康摘要的 `quality_signals`。

## 验收证据

- `npm run build` 通过。
- `PYTHONPATH=..:.:src pytest -q tests/test_knowledge_bases.py -k reader` 通过。
- `npm run e2e:multi-kb-ask` 通过，使用 AuthNode/Gateway browser session 覆盖多 KB hard scope、ReaderPane 高亮、source-focused follow-up、Ask -> Writing Brief、Evidence Brief Library Wiki page content/taxonomy edits and revision restore、Evidence Brief Library 生命周期/重新生成、Graph Ask 健康摘要、Graph Ask -> Writing evidence、Graph selected node -> Writing evidence、Review evidence comparison/ReaderPane、Review candidate comparison、Review snooze/restore、Review 批量拒绝、Review approve/apply、applied lineage 和 applied relationship -> Graph inspection；临时 alpha/beta/review/graph/writing 数据清理后无残留。
- `npm run e2e:writing -- --list` 通过，确认 Writing browser e2e spec 可发现；该 spec 已覆盖 Writing 健康指示与持久化质量信号。
- `./scripts/pska-phase1-multikb-release-gate` 通过，覆盖 PSKA core `494 passed`、frontend build、FastReAct contracts `75 passed`、PSKA/FastReAct `git diff --check`。
- `./scripts/pska-fastreact-kb-scope-smoke --timeout-seconds 240` 通过，使用真实 PSKA service、真实 FastReAct daemon、DeepSeek `deepseek-v4-flash` 和 PSKA HTTP MCP；FastReAct 实际调用 `pska_pska_search` / `pska_pska_read_evidence_context` 并保留 hard KB/source scope，临时 alpha/beta 数据清理后 residue 全 0。

真实浏览器验收仍需在集成系统启动后显式运行：

```bash
cd "$PSKA_REPO"
./start.sh

cd "$PSKA_REPO/frontend"
PSKA_E2E_TENANT_ID="tenant_graphintell" \
PSKA_E2E_USER_ID="test_user" \
PSKA_E2E_PASSWORD="<local AuthNode password>" \
  npm run e2e:multi-kb-ask
```

## 下一步

- 继续收紧 Ask/Reader/Writing 的细节验收：补充 ProcessingTimeline、Writing/Review/Graph 健康摘要的真实运行截图核对，并把这些入口的空状态、错误状态和移动端布局做成更完整的浏览器验收。
