# 里程碑：Reader/Ask 产品切片

本里程碑承接 Phase 1 多知识库 RAG 底座。它是一个 Reader/Ask 产品切片，
不是整体产品路线里的 Phase 2。整体 Phase 2 的计划来源是
[PSKA Phases](PHASES.md) 和 [RFC 0002: Multi-evidence Composition](rfcs/0002-multi-evidence-composition.md)。

本切片目标是把 Ask 的引用从“可见 citation”升级为“可检查、可回到原文、
可基于原文继续追问”的读问闭环。这些能力仍然有用，并作为 Phase 2 Evidence
Composition 的产品层支撑能力复用。

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

截至当前实现，该 Reader/Ask 产品切片已落地：

- Ask 结果中的 `EvidenceWindow` 已升级为 citation inspector，展示 citation 坐标、KB 归属、source window policy、score、原文字符范围、URL 和原文窗口。
- 新增 `GET /workspace/reader/source`，按 `source_item_id` 返回当前用户可见且 KB scope 覆盖的 source、documents、chunks、passage windows 和 `scope_applied`。
- Citation inspector 增加“查看原文”动作，拉取 reader source 后在同一侧栏展开 ReaderPane，展示原文正文和按 ordinal 排列的 chunks。
- ReaderPane 已把 citation 的 `source_window.start_char/end_char` 或 source window 文本映射回原文正文与 chunk，打开原文时会围绕引用位置截取上下文并高亮命中段落。
- ReaderPane 已增加原文定位状态条：打开原文后展示“已定位引用 / 原文已打开”、KB lineage、source/document/chunk/passage 坐标以及文档/chunk 计数，帮助用户判断 citation 是否真的落回了原文位置。
- Citation inspector 增加“追问这段”动作。
- Today/Ask composer 会把选中的 citation 转成下一问草稿，并显示可清除的原文焦点 chip。
- 下一问会把该 citation 的 `source_item_id` 注入 Ask `scope.source_item_ids`，避免从原文继续问时范围回退到整个 KB。
- ReaderPane 已支持选中原文后“追问选区”：选区文本会被包装为同一套 evidence ref，保留 source/document/chunk/passage 与 KB lineage，并继续注入 `source_item_ids` hard scope。
- Today Ask 结果已增加“生成 Brief”动作：有 `run_id` 且带可检查引用的 Ask run 可以直接生成 Evidence Brief / Writing board，保留 Ask run lineage、citations/source_refs 和 KB lineage，成功后进入 Writing。
- Writing 入口页已增加 Evidence Brief Library：按真实 `metadata.kind=evidence_wiki_brief` 的 Writing board 列出 Brief，详情展示 lineage、KB scope、review 状态和 source refs，并可通过现有 Writing/Evidence Brief API 执行重新生成、过期、回滚与恢复。
- Citation inspector 已复用到 Writing answer/evidence/draft 节点和 Graph 节点 / Graph Path citations；这些入口共享同一套 citation 坐标、KB lineage、ReaderPane 原文加载和高亮能力。
- no-answer diagnostics 已从维度列表升级为“原因 + 建议下一步”的行动面板，按通用状态提示扩大范围、检查索引、补证据、重试 Deep Ask/MCP 或检查权限/策略。
- `ProcessingTimeline` 已从原始事件日志升级为五阶段摘要，合并 `progress`、`agent_steps`、agentic trace 和 `quality_signals`，展示理解、检索、读取、生成、证据校验的状态、证据数、引用数和质量状态；原始过程仍保留为可展开明细。
- `AskResult` 已复用同一套 Ask 健康摘要：Today/Knowledge Base/Graph 中的普通 Ask 结果会在 scope 与 timeline 之前显示“处理中 / 有引用 / 证据不足 / 缺引用 / 失败”等状态，和 Writing/Review/Graph 的健康标签保持同一判断口径。
- Writing 节点和 Composer 的 Ask timeline 已复用同一套阶段摘要，运行中的 preview 与持久化的 `last_ask` 都会携带 `progress`、`evidence_check` 和 `quality_signals`。
- Writing answer/evidence/draft 节点顶部已增加 Ask 健康指示，把 `quality_signals`、`evidence_check`、节点状态和引用数提炼成“有引用 / 证据不足 / 需复核 / 失败 / 无需证据”等短标签，展开后仍可查看完整阶段和原文引用。
- Review Center 列表已复用同一类证据健康摘要，把 `source_ref_status`、`quality_tier`、`review_eligible` 和 apply readiness 提炼成“可审核 / 缺证据 / 仅诊断 / 需检查”等短标签。
- Review Center 卡片已增加“证据对比”折叠区，复用 citation inspector / ReaderPane 展示每条 Review source ref 的 KB 归属、source/document/chunk/passage 坐标、预览文本和原文读取能力。
- Review Center 已增加批量选择工具条：pending 列表可批量批准/拒绝，approved 列表可批量应用；批量动作仍逐条调用真实 Review API，并在每张卡片上保留处理状态。
- Review Center 已增加“候选对照”面板：选择两条以上 Review candidate 时，主区会基于真实候选数据横向比较状态、建议、证据健康、KB lineage、置信度、引用数、source 数和共享 source refs。
- Review Center 已增加“稍后”队列：pending Review 可以单条或批量 snooze 到 `snoozed` 状态，`snoozed` tab 可以单条或批量恢复到 pending；后端 `ReviewService` 会写入 `review.snooze` / `review.restore` audit event。
- Review Center 的 applied 卡片已增加“应用 lineage”面板，基于真实 `application_result` 展示写入目标、审计动作、状态、target id 和应用时保留的证据引用数量。
- Review Center 已把 KB 过滤从隐式请求参数升级为可见状态条，展示当前范围名称、scope 模式、KB 数量以及当前状态下的候选数、有证据数、类型数和已选择数。
- Today 右侧 Review 卡片已接入同一类证据健康摘要，把待审候选在日常入口中标成“需复核 / 补引用 / 缺证据”，减少用户必须先进入 Review Center 才知道证据状态的跳转。
- Ask 结果已把 `route.scope_applied` / `scope_applied` 从 header pill 升级为可见状态条，展示本次范围名称、scope 模式、retrieval owner、KB 数、source 限定数、可检索 KB 数和 readiness warning 数。
- Corpus summary 已增加 `当前 KB / 全部资料` 切换：当前视图展示当前知识库条目、原文、chunk、向量覆盖和高级源计数，全部视图聚合 active KB 的条目、原文、chunk 和全局高级源计数。
- 资料库侧栏已从普通说明升级为轻量 KB tree：当前 KB、置顶 KB、默认 KB 优先展示，并显示 readiness、资料数和 chunk 数；点击只切换当前 KB context，不新建并行知识库产品入口。
- Ask scope selector 的多知识库菜单已支持搜索 KB，并在每个候选项上展示可检索状态、资料数、chunk 数和 embedding 覆盖率，便于发问前确认范围 readiness。
- Today Ask composer 已把只读 KB 范围提示升级为可操作 scope picker：附件按钮旁可切换当前 KB、全部资料库或多选 KB，并继续把选中 scope 透传给 Ask run。
- Knowledge Base 详情页已按产品心智收敛为 `资料 / Ask / 处理 / Digest / Graph / Writing / 设置` tabs：复用真实导入、文档 lifecycle、证据搜索、chunk preview、高级同步、Digest 日志、当前 KB Graph/Memory 摘要、当前 KB Writing boards、当前 KB scoped Writing board 创建和 Prompt Profile/KB 管理入口，避免资料库工作台继续平铺成一长页。
- Knowledge Base `处理` tab 已升级为当前 KB Processing cockpit：把资料、原文、chunks、处理状态、失败数、索引、向量覆盖和 readiness 聚合在一个状态面板，并提供同步高级源、加入资料、去 Ask、去 Digest、刷新等真实动作。
- Knowledge Base `Ask` tab 已增加内联当前 KB Ask 面板：直接调用真实 `askWorkspaceStream` quick RAG，默认 hard-scope 到当前知识库，并复用 AskResult 的 scope 状态、ProcessingTimeline、citation inspector、ReaderPane 和生成 Brief 能力。
- Knowledge Base `Ask` tab 已增加 readiness-aware preflight：在发问前展示当前 KB 资料数、chunks、向量覆盖和 hard scope 模式；空 KB/未切片/处理失败/索引待刷新都会给出通用行动入口，可跳到资料、处理或 Today，且不阻断真实 Ask/no-answer diagnostics。
- Knowledge Base `Ask` tab 已增加当前 KB scoped Ask 历史：复用真实 Ask conversations 的 `scope_applied/knowledge_base_ids`，展示当前 KB 的最近对话、强限定数量和最近提问时间，并可直接创建新的当前 KB hard-scope Ask 对话或跳回 Today 继续既有对话。
- Knowledge Base `Digest` tab 已升级为当前 KB Digest cockpit：展示 scope 内资料、chunks、Digest/Claims/Review、失败数、最近任务与 readiness，并提供运行 Digest、查看处理、进入 Review、打开 Writing、从可用 Digest 生成 Brief 的真实动作。
- Knowledge Base `Graph` / `Writing` tab 已把空状态升级为可操作状态：空 Graph 会引导到处理或 Graph 工作区；空 Writing 会引导到当前 KB Ask、新建绑定 KB scope 的 Writing board 或打开 Writing 工作区，避免空 tab 只停留在说明文字。
- Graph 工作区已把当前 KB scope 从隐式请求参数升级为可见状态条，展示范围名称、scope 模式、KB 数量以及当前过滤后的节点、边、source、document 计数。
- Graph Ask 主路径和 legacy Graph Path 面板都已增加证据健康摘要，把 citations、agentic repair 和错误状态提炼成“有引用 / 缺引用 / 已重写 / 需复核 / 失败”等短标签。
- Linking digest 生成的 `relationship_candidate` Review proposal 已携带 `relation_type`、成员、证据摘要和 source refs，Review Center 的 apply-ready 判断会校验真实 apply 所需字段，避免“看起来可应用但应用失败”的候选。
- Review Center 的 applied relationship item 已持久返回 `application_result.target_ids.created_hyperedge_id`，并提供“在 Graph 查看”动作，点击后会切到 Graph、拉取对应 `hyperedge:<id>` 的 1-hop subgraph 并打开节点 inspector。
- Graph Ask 已增加“保存到 Writing”动作：即时 Graph Ask 没有持久化 `ask_run_id` 时，也会用真实 Writing API 创建 Graph Brief board、answer/evidence 节点和 citation edge，并自动打开刚生成的 board。
- Graph 节点详情已增加“保存证据”动作：选中的 source/document/passage/claim 等带 `source_refs` 节点可以直接创建 Graph Node Writing board、evidence 节点和章节引用边，保留 graph node metadata 与 citation lineage。
- Writing question 节点已显示当前 board 的 Ask 范围，运行 Writing follow-up Ask 时会把 board 绑定的 KB scope 写入 `last_ask.scope`，避免画布里的追问悄悄脱离知识库边界。
- Evidence Wiki page preview 已增加页级内容编辑、内容修订恢复、内容发布同步状态和 durable taxonomy：页面标题/摘要/正文保存到 Writing board 与 managed `evidence_wiki_page_body` 节点，每次保存会留下 `wiki_content_revisions` 快照并可恢复旧修订；已发布页面被编辑后会标成“待更新发布”，再次 publish 会把当前 revision 记录为已发布内容；tags/categories/topics/collections 保存到 board metadata，Wiki search 可按 taxonomy facet 过滤，相关页面会纳入共享 taxonomy。
- `frontend/e2e/multi-kb-scoped-ask.spec.ts` 已扩展真实浏览器验收：Knowledge Base 详情 tabs 可切换到 `资料 / Ask / 处理 / Digest / Graph / Writing / 设置` 并显示对应真实入口；资料库 summary 可在当前 Alpha KB 和全部资料之间切换；空 KB 的 Knowledge Base `Ask` preflight 会显示 hard scope、0 chunks、“还没有可问的资料”，并可跳到资料入口或 Today；Today Ask composer 的 `today-scope-picker` 会显示 Alpha KB，并能在当前/全部之间切换后回到当前 KB；UI Ask 展示 alpha `ask-result-health`、`ask-scope-status`、citation inspector，点击“查看原文”后 ReaderPane 包含 alpha 原文且不包含 beta secret；在 ReaderPane 选中 alpha 高亮原文并点击“追问选区”后，输入框包含 alpha `source_item_id` 和选区文本；点击“生成 Brief”后会进入 Writing，最新 brief 节点包含 alpha 证据且不包含 beta secret；返回 Evidence Brief Library 后会验证该 Brief 出现在列表/详情中，保存两版 Wiki 页级正文、看到“待更新发布”、从 revision list 恢复第一版并点击“更新发布”回到“已同步发布”，保存 taxonomy 并用 facet 过滤，执行过期、恢复、回滚、恢复，并通过 lineage 重新生成 Brief，确认 alpha 证据仍不泄露 beta；回到 Knowledge Base `Ask` tab 后会验证当前 KB Ask 历史出现刚才的 scoped Ask 对话、标记为强限定，点击既有对话或新建当前 KB Ask 都会回到 Today 并保持当前 KB scope；Knowledge Base 内联 Ask preflight 会显示 hard scope readiness，内联 Ask 会在 Alpha KB hard scope 下返回 alpha 证据、不泄露 beta secret，并在 390px 移动端视口继续显示 preflight、Ask 健康摘要和 ProcessingTimeline；Graph 工作区会显示当前 Alpha KB 的 `graph-scope-status`，Graph Ask 在空图/有图主路径也会显示 `graph-path-evidence-health` 与通用 `ask-result-health` 且不泄露 beta 证据，点击“保存到 Writing”后进入 Graph Brief board 且 answer/evidence 节点保留 alpha citation；Graph 本地搜索选中 alpha 节点后，点击“保存证据”会进入 Graph Node board 且 evidence 节点保留 alpha 原文、不包含 beta secret；真实 linking digest 生成的 alpha Review candidate 会在 Today 右栏显示 `today-review-evidence-health`，在 Review Center 显示 `review-scope-status` 和 `review-evidence-health`，同时另一个真实 beta Review candidate 在 alpha KB scope 下不可见，并能打开“证据对比”后从 Review citation inspector 进入 ReaderPane 检查原文；一个真实 Review candidate 会被 snooze 到 `snoozed` tab 并恢复回 pending；额外两个真实 Review candidates 会被批量选择，出现“候选对照”横向比较面板后再批量拒绝；主 Review candidate 点击“批准并应用”后会进入已应用列表、显示“应用 lineage”里的 Graph relationship target id 和证据数量、创建 graph relationship，并能从 applied card 打开 Graph inspector 查看 `shared_topic` hyperedge。
- 同一 spec 已补充 ReaderPane 定位验收：Ask citation 和 Review citation inspector 打开原文后会显示 `reader-focus-status`，包含定位状态、source/docs/chunks 坐标；Ask ReaderPane 在 390px 移动端视口下仍能看到定位状态与高亮原文。
- 同一 spec 已补充空 KB 联动空态验收：Processing tab 的 `knowledge-base-processing-panel` 会显示当前 KB 处理 cockpit 与资料/Ask/Digest 跳转；Digest tab 的 `knowledge-base-digest-empty` 会显示当前 KB cockpit、禁用空库 Digest 动作并可跳到资料/处理；Graph tab 的 `knowledge-base-graph-empty` 可跳到处理或 Graph 工作区，Writing tab 的 `knowledge-base-writing-empty` 可跳到 Ask、新建当前 KB 画布或打开 Writing 工作区。
- `frontend/e2e/writing-workspace.spec.ts` 已扩展真实浏览器验收：资料库侧栏 KB tree 会显示并激活临时 KB；Writing toolbar 和 question 节点会显示 board Ask scope；Writing answer/question 运行后应出现 `writing-node-ask-health`，并且持久化节点/`last_ask` 保留可支撑健康摘要的 `quality_signals`；如果 board 绑定了 KB，所有 Writing follow-up Ask 的 `last_ask.scope.knowledge_base_ids` 必须保留同一 KB scope。

## 验收证据

- `npm run build` 通过。
- `PYTHONPATH=..:.:src pytest -q tests/test_knowledge_bases.py -k reader` 通过。
- `npm run e2e:multi-kb-ask -- --list` 通过，确认真实浏览器 e2e spec 可发现且不会在 discovery 阶段要求本地密码。
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
