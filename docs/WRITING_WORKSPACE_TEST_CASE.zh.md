# Writing Workspace / Inquiry Graph 测试用例

## 目标

验证“写作 = 构造可追溯的问题-答案网络，再把答案节点组织成文章”的完整链路：

- 一个写作项目对应一块独立画布。
- 每个 question 节点有独立 Ask session。
- 与 question 节点直接相连的节点会作为结构化上下文传给 Ask PSKA。
- 多个 question 节点可以并行运行。
- 点击/展开节点可以查看该节点自己的事件流。
- Ask 完成后生成 answer/evidence/gap 子节点。
- 用户把 answer 节点加入 section，再生成 draft，最终导出 Markdown。

## 准备数据

PSKA 只能通过项目根目录 `./start.sh` 启动。不要单独启动前端或后端。

启动后，优先使用真实 E2E 从 workspace 文件开始验证：

```bash
./scripts/pska-writing-workspace-e2e \
  --config "/Users/xudawei/Documents/personal archive/.pska/config.json"
```

这条链路会把虚构资料写入：

```text
~/PSKA_workspaces/tenants/<tenant>/users/<user>/notes/e2e-writing-<run_id>/
```

然后显式执行 `knowledge-source add-folder`、`files-sync`、`digest-now`，再用浏览器登录 PSKA 并创建 Writing 项目。旧的 `writing-demo-seed` 只适合快速手动演示；完整验证应使用 E2E，因为它能覆盖“向 workspace 放数据 -> 登录后看见语料库 -> digest -> 写作”的真实路径。

## 推荐测试流程

1. 打开 `http://127.0.0.1:5173`，进入“写作”。
2. 打开 `Northstar Robotics Q3 reserve-allocation memo` 项目。
3. 并行点击前 5 个 question 节点的 `Ask`：
   - Northstar Robotics 的业务、阶段和融资背景是什么？
   - Q3 reserve-allocation shortlist 的判断标准是什么，Northstar 对应哪些维度？
   - Northstar 的产品牵引、客户试点和商业化信号是否足够？
   - Northstar 的单位经济、毛利、现金消耗和 runway 暴露了什么风险？
   - 反对把 Northstar 纳入 Q3 shortlist 的最强理由是什么？
4. 展开任意 question 节点，确认节点内出现自己的 agentic 事件流。
5. Ask 完成后，检查画布上新增的 answer/evidence/gap 子节点。
6. 运行 diligence 追问节点：
   - 如果考虑纳入 shortlist，应该设置哪些条件和下一步 diligence？
7. 把选中的 answer 节点加入右侧章节：
   - 背景与判断标准
   - 支持证据
   - 风险与反方观点
   - 建议与下一步 diligence
8. 点击“生成章节草稿”。
9. 点击“导出 Markdown”，检查文章是否包含章节、正文、引用和缺口/冲突提示。

## 预期产品行为

- 用户不需要选择 direct/agentic/GraphRAG/FastReAct 等工程模式。
- 写作入口仍然只通过 Ask PSKA 提问。
- 节点可以并行运行；每个节点保留独立 session 和事件流。
- 追问节点会把直接连接的节点摘要作为上下文传入 Ask，而不是从全局 UI 偷拿上下文。
- compose 只基于已选 answer 节点生成草稿，不重新检索。
- Demo 数据只是测试语料；不能在代码里加入 Northstar、reserve-allocation 等特例规则。
