# PSKA Core

PSKA Core 负责私有优先的知识模型、ACL 规则、记忆对象、超图原语和检索接口。

当前 MVP 状态跟踪在 [`docs/mvp-status.md`](docs/mvp-status.md) 中。
下一阶段实现 TODO 跟踪在 [`docs/roadmap-todo-zh.md`](docs/roadmap-todo-zh.md) 中。

核心有意与通道采集器分离。`channels/twitter-x` 等通道项目采集和规范化原始素材；PSKA Core 注册、索引、搜索并治理对这些素材的访问。

## 存储方向

生产目标是使用 `pgvector` 的 PostgreSQL。`src/pska_core/migrations/001_init.sql` 中的迁移定义了 v1 schema。Python 服务包括一个内存实现，供测试和早期代理集成使用。

## 隐私规则

- 用户和团队是匿名标识符
- 真实姓名、亲属标签、别名、密钥和本地路径必须远离已提交的配置
- 知识默认为私有
- 团队可见性通过 `visible_team_ids` 显式设置
- 代理生成的记忆属于被代表的用户，绝不属于 `agent_service` 身份

## 本地测试

```bash
./.pska/venvs/pska-py312/bin/python core/scripts/e2e_smoke.py
```

测试脚本会重置 `pska_smoke`，导入 `~/Downloads/twitter_archive/*.zip`，运行 LLM 提取、CLI 搜索、代理式搜索、HTTP API 检查、直接 MCP 检查和 Fastreact MCP 加载。

## Twitter Zip 导入

```bash
./scripts/pska db-reset --name pska_smoke
./scripts/pska --database-url postgresql:///pska_smoke \
  import-twitter-zips \
  --input ~/Downloads/twitter_archive \
  --archive-root archive/imports
```

规范的新归档应使用 `pska.archive.v2`。旧版 Twitter zip 元数据仅作为兼容路径接受。

## BGE-M3 嵌入

PSKA 的 P0 嵌入路径使用本地 BGE-M3，通过 `FlagEmbedding` 运行，并把 1024 维向量写入 PostgreSQL `pgvector`。

使用仓库 wrapper，让命令统一运行在本地 Python 3.12 环境：

```bash
brew install python@3.12
./scripts/bootstrap_pska_env
```

对已导入 chunk 做 backfill：

```bash
./scripts/pska --database-url postgresql:///pska_smoke \
  embed-backfill \
  --embedding-provider bge-m3
```

启用 vector retrieval 的 hybrid search：

```bash
./scripts/pska --database-url postgresql:///pska_smoke \
  search \
  --query "agentic coding automation" \
  --embedding-provider bge-m3
```

设置 `PSKA_EMBEDDING_PROVIDER=bge-m3` 后，CLI/API/MCP 检索会使用同一套 BGE-M3 provider。如果 provider 加载失败，命令会显式失败，不会静默退回 lexical search。

## Fastreact MCP 边界

Fastreact 可以将 PSKA 加载为只读 stdio MCP 服务器，而无需导入 PSKA 内部：

```bash
export PSKA_DATABASE_URL=postgresql:///pska_smoke
export FASTRACT_MCP_SERVERS='[{"name":"pska","command":"./scripts/pska","args":["mcp-server"],"isolation":"shared"}]'
```

API 密钥应仅注入到 Fastreact 进程环境中。不要将它们写入仓库配置。
