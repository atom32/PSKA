# PSKA 文档索引

这是当前文档地图。它把用户入口、开发/运维参考、架构/API 参考和历史归档分开，避免旧 milestone 文档继续充当当前计划。

## 快速开始

- [根 README](../README.zh.md)：多租户开发入口、服务地图、身份 headers、source/digest/Ask 工作流和验证命令。
- [Developer Quickstart](DEVELOPER_QUICKSTART.md)：开发栈启动、首次配置、cold-start 检查和 FastReAct 联动。
- [发布、初始化与 FastReAct 指南](RELEASE_INIT_FASTREACT_GUIDE.zh.md)：本地初始化、日常启动和真实 FastReAct 联调流程。
- [配置约定](CONFIGURATION_CONTRACT.zh.md)：PSKA、FastReAct、AuthNode 的配置文件位置、启动入口和兼容字段。

## 日常使用

- `./start.sh` 启动本地后端 supervisor 和前端 Workspace。
- `./scripts/pska --config .pska/config.json digest-now` 同步 files/Twitter archive 并处理一次 digest。
- `./scripts/pska --config .pska/config.json daily-status` 查看 deterministic readiness 和 backlog 状态。
- `./scripts/pska --config .pska/config.json review-list --status pending --summary` 查看待人工处理的 review。

当前 digest scheduler 是增量轮询：local daemon 默认每 300 秒检查一次有没有新 source 或改动 source；它不是每天固定一次的 cron。

目录级资料包可通过 `.pska-source.json` 显式声明为一个 source collection；用法见 [Operations Runbook](../core/docs/operations-runbook-zh.md#source-collection-marker)。

## 架构与 API

- [Architecture](ARCHITECTURE.md)：当前系统形态、source-centric 数据流、discovery 规则和 scheduler 行为。
- [Evidence-driven QA Engine](PSKA_EVIDENCE_QA_ENGINE.zh.md)：Ask/RAG 的 Retrieval → Evidence → Citation → Answer 流水线、audit schema、timeline 和回归策略。
- [API Reference](API_REFERENCE.md)：Workspace、CLI 和 integrations 使用的 HTTP endpoint。
- [Feature Reality Check](FEATURE_REALITY_CHECK.md)：已实现、部分实现和 design-only 能力边界。
- [WeKnora 核心覆盖验收](WEKNORA_COVERAGE.zh.md)：多租户核心替代能力、E2E 脚本和竞品对照验收口径。
- [Telemetry Design](TELEMETRY.md)：design-only telemetry 说明。
- [Product Design](../core/docs/product-design-zh.md)：产品定位和用户工作流。
- [Architecture Status](../core/docs/architecture-status-zh.md)：模块成熟度和主要缺口。
- [Vision](../core/docs/vision-zh.md)：长期愿景。

## 运维

- [Operations Runbook](../core/docs/operations-runbook-zh.md)：local daemon、数据库、状态、jobs 和恢复命令。
- [Online Service Contract](../core/docs/service-contract-zh.md)：HTTP service、auth/request context、jobs、candidates、connectors 和 digest API。
- [配置约定](CONFIGURATION_CONTRACT.zh.md)：三系统本地配置路径、旧字段和冗余项说明。
- [企业认证网关](ENTERPRISE_AUTH_GATEWAY.zh.md)：AuthNode + PSKA Gateway 的正规浏览器登录、session 和反向代理流程。
- [FastReAct Boundary](../core/docs/fastreact-agentic-boundary-zh.md)：PSKA/FastReAct 职责边界。
- [FastReAct Protocol](../core/docs/fastreact-protocol-zh.md)：层间协议细节。
- [FastReAct Real Integration Manual](../core/docs/fastreact-pska-real-integration-manual-zh.md)：真实本地联调流程。

## 前端

- [Frontend README](../frontend/README.md)：User Workspace 启动方式和页面。
- [Backend Feature Map](../frontend/BACKEND_FEATURES.md)：前端页面与后端能力映射。

当前前端状态：User Workspace 已包含 Today、Discoveries、资料库、多轮 Ask、Graph、review-oriented flows、Prompt Profiles 和 Writing/Evidence Brief surfaces。普通用户入口支持上传文件、粘贴文本、添加 URL/RSS；folder source 保留给 admin/dev、本地迁移或批量导入。代码内部仍然沿用 KnowledgeSource、source item、document、chunk、digest、review 等模型。

## Twitter/X

- [Twitter/X Channel README](../channels/twitter-x/README.md)
- [Twitter/X Channel README zh](../channels/twitter-x/README.zh.md)
- [Archive Schema v2](../channels/twitter-x/docs/schema.md)

## 历史归档

以下文档只作为历史参考，不再是当前计划来源：

- [MVP Status](archive/core/mvp-status.md)
- [MVP Status zh](archive/core/mvp-status-zh.md)
- [MVP User Scope zh](archive/core/mvp-user-scope-zh.md)
- [Roadmap/TODO zh](archive/core/roadmap-todo-zh.md)
- [Todo Implement System zh](archive/core/todo-implement-system-zh.md)
