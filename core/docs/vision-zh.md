# PSKA 愿景

日期：2026-06-12

## 定位

PSKA 是用户授权范围内的一切个人上下文的私有知识底座。它持续理解、整理、摘要、关联、提醒，并通过 ACL 和用户意图边界安全地服务于用户本人和被授权的 agent。

PSKA 不只是一个被动问答知识库。长期目标是成为一个 personal context operating layer：

```text
授权数据源 -> 持续摄入 -> 空闲 digest -> 结构化记忆 -> 主动 agentic service -> 用户确认 / 回写
```

## 授权范围

PSKA 应该能在用户明确授权后掌握并整理这些来源：

- 本机文件，这是最基础的管理目标。
- 邮件、日历、联系人和待办。
- 照片、视频和相册。
- 浏览器页面、书签、阅读历史和网页剪藏。
- Git repo、issue、PR、commit、工作日志。
- NAS、外接硬盘和归档目录。
- Home Assistant 与家庭设备事件。
- 用户的交际网络、组织关系和项目关系。
- 各个平台上的对话历史，包括聊天、社交平台、论坛和工作协作工具。

这些来源不是简单地堆进搜索索引，而是需要保留来源、时间、权限、版本、引用和可追溯证据。

## 核心原则

- 私有优先：数据默认属于用户，权限默认收紧。
- 授权优先：connector 必须表达用户授权范围，不能默认扫全盘。
- ACL-first：先判断可见性，再检索、摘要和回答。
- 来源可追溯：回答、摘要、记忆和主动建议都应能追溯 source refs。
- 后台 digest：系统应在空闲时自动整理内容，类似 Apple Photos 在空闲时识别人、地点和事件。
- 人在回路：高影响更新、对外动作、删除、合并、公开分享必须进入 review / approval。
- Agent 可用：PSKA 应该能被 FastReAct 或其他 agentic service 通过稳定 API/MCP 使用，但 agent 不能绕过 PSKA 权限判断。

## 能力分层

```text
Connectors
  files / mail / photos / browser / git / NAS / home / social / conversations

Ingestion
  scan / import / dedupe / version / source refs / permission inheritance

Digest
  idle jobs / summaries / entities / people / projects / places / events / relationships

Memory Model
  source items / documents / chunks / entities / hyperedges / memories / profile cards / timelines

ACL and Consent
  user identity / represented user / team visibility / source permissions / approval policy

Agentic Service
  search / briefing / reminders / project tracking / file organization / review suggestions

Interfaces
  CLI / HTTP API / MCP / UI / local automation / FastReAct / future clients
```

## 主动式服务

PSKA 的主动能力应围绕“用户会希望系统知道并适时提醒的事情”，而不是无限制地替用户行动。

第一批主动服务候选：

- 每日/每周 briefing：最近文件、邮件、对话、项目变化和待处理事项。
- 项目雷达：从 git、文件、浏览器和对话中发现同一项目的状态变化。
- 文件整理建议：发现重复文件、长期未整理目录、可归档资料。
- 照片和事件 digest：人物、地点、事件、旅行、家庭场景的后台聚类。
- 邮件/对话待办提取：只生成候选，不自动对外回复。
- 关系和上下文提醒：在用户准备联系某人、参加会议或打开项目时提供相关背景。

## Online Service 意义

PSKA 成为 online service 不是为了替代 CLI，而是为了支撑常驻、后台、跨来源和主动能力：

```text
clients / FastReAct / UI / automations
        -> PSKA Online Service
        -> jobs / workers / Postgres + pgvector / ACL / digest / memory graph
```

CLI 继续存在，但角色转为管理、调试、迁移和 service client。

## 与 FastReAct 的边界

PSKA 负责：

- 用户授权数据的摄入、存储、权限、索引和 digest 机制。
- ACL、source refs、citations、review、memory graph。
- digest job、lease、幂等写入、候选记忆、review queue、audit event。
- 主动服务产生的候选事件和需要用户确认的 action。

FastReAct 负责：

- 所有 agentic loop 行为，包括 planning、工具编排、模型调用、session runtime 和事件流。
- 作为 PSKA 的 agentic 服务层执行 digest、briefing、主动提醒等工作流。
- 消费 PSKA 的 HTTP API 或 MCP tools。

边界原则：PSKA 是基础设施和机制，FastReAct 是可复用的 agentic 执行层。FastReAct 不直接访问 PSKA DB，不做 PSKA 知识 ACL 决策；未来其他系统也应通过同一套 PSKA API/MCP 服务契约接入。
