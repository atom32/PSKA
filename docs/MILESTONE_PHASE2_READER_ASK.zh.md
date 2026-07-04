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
- Citation inspector 增加“追问这段”动作。
- Today/Ask composer 会把选中的 citation 转成下一问草稿，并显示可清除的原文焦点 chip。
- 下一问会把该 citation 的 `source_item_id` 注入 Ask `scope.source_item_ids`，避免从原文继续问时范围回退到整个 KB。
- `frontend/e2e/multi-kb-scoped-ask.spec.ts` 已扩展真实浏览器验收：UI Ask 展示 alpha citation inspector，点击“追问这段”后输入框包含 alpha `source_item_id`，并保持 beta secret 不出现在结果中。

## 验收证据

- `npm run build` 通过。
- `npm run e2e:multi-kb-ask -- --list` 通过，确认真实浏览器 e2e spec 可发现且不会在 discovery 阶段要求本地密码。
- `./scripts/pska-phase1-multikb-release-gate` 通过，覆盖 PSKA core `490 passed`、frontend build、FastReAct contracts `75 passed`、PSKA/FastReAct `git diff --check`。

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

- ReaderPane：从 citation inspector 进入更完整的 source/document 阅读面板。
- 原文定位：把 `source_window.start_char/end_char` 与文档正文片段高亮关联起来。
- CitationInspector 复用到 Graph/Writing 引用展示。
- no-answer diagnostics 进一步压缩成用户能直接行动的修复建议。
