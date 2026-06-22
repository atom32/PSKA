# PSKA

个人社交知识归档（Personal Social Knowledge Archive）—— 一个以隐私优先的知识管理系统，具备 LLM 辅助提取、超图记忆和 ACL 治理的检索功能。

## 概述

PSKA 是一个端到端的个人知识归档系统，可以：

- **采集** 来自社交平台的内容（目前支持 Twitter/X）
- **标准化** 内容到统一的 schema（`pska.archive.v2`）
- **存储** 原始数据到 PostgreSQL，采用隐私优先的 ACL 策略
- **提取** 通过 LLM 提取实体和关系
- **构建** 超图记忆模型
- **检索** 使用 LLM 规划的代理式搜索
- **暴露** 结果通过 CLI、HTTP API 和 MCP

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                        PSKA 系统                             │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  channels/               core/                               │
│  ┌────────────────────┐       ┌──────────────────────┐      │
│  │  twitter-x/        │ ──>   │  知识存储              │      │
│  │    Chrome 扩展     │  ZIP  │  PostgreSQL+pgvector  │      │
│  │    Python CLI      │       │  ACL / 隐私           │      │
│  │    schema v2       │       │  LLM 提取             │      │
│  └────────────────────┘       │  超图记忆             │      │
│                               │  检索服务             │      │
│                               │  MCP 服务器           │      │
│                               └──────────────────────┘      │
│                                      │                       │
│                                      v                       │
│                               ┌──────────────┐               │
│                               │  Fastreact   │               │
│                               │  (前端)       │               │
│                               └──────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

## 组件

### channels/twitter-x/

Twitter/X 采集通道，包含 Chrome 扩展和 Python CLI。

- **Chrome 扩展** (`extension/`)：在已登录的浏览器中归档推文
- **Python CLI** (`src/pska/`)：命令行采集工具
- **Schema** (`docs/schema.md`)：PSKA v1 元数据规范

[→ Twitter-X 通道 README](channels/twitter-x/README.zh.md)

### core/

PSKA Core 实现知识模型、存储和服务。

- **数据模型**：用户、团队、空间、来源、文档、分块、实体、超边
- **隐私**：匿名 ID、私有优先可见性、团队 ACL
- **LLM 集成**：实体/超边提取、代理式搜索
- **API**：CLI、HTTP、stdio MCP

[→ PSKA Core README](core/README.zh.md) | [→ MVP 状态](core/docs/mvp-status.md)

## 快速开始

### 本地发布和初始化

完整流程见 [发布、初始化与 FastReAct 联动指南](docs/RELEASE_INIT_FASTREACT_GUIDE.zh.md)。
首次配置、日常启动和 FastReAct 真实联调都以这份文档为准。

### 运行环境

PSKA 统一使用本地 Python 3.12 虚拟环境，这样 core、Twitter/X channel 和 BGE-M3 embedding 栈保持可迁移：

```bash
brew install python@3.12
./scripts/bootstrap_pska_env
mkdir -p .pska
cp core/config.pska.example.json .pska/config.json
```

编辑 `.pska/config.json`。这是唯一的本机配置文件，数据库、workspace、HTTP service、LLM、FastReAct、embedding、files roots 和启动行为都写在这里；不要再用 `PSKA_*` 环境变量配置启动。

最小结构：

```json
{
  "database": { "url": "postgresql:///pska" },
  "workspace": { "root": "~/PSKA_workspaces/default" },
  "startup": {
    "bootstrap": true,
    "backend": true,
    "frontend": { "enabled": true, "host": "127.0.0.1", "port": 5173 }
  },
  "service": { "host": "127.0.0.1", "port": 8765, "service_token": null },
  "llm": {
    "api_key_file": "~/api_key.txt",
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "timeout_seconds": 60
  },
  "fastreact": {
    "url": "http://127.0.0.1:8000",
    "service_token": null,
    "timeout_seconds": 30
  },
  "agentic_service": {
    "provider": "fastreact",
    "url": "http://127.0.0.1:8000",
    "service_token": null,
    "timeout_seconds": 30
  },
  "embedding": {
    "provider": "disabled",
    "model": "BAAI/bge-m3",
    "dimensions": 1024,
    "batch_size": 16
  },
  "ingest": { "chunk_size": 1200, "chunk_overlap": 0 },
  "files": {
    "roots": ["~/PSKA_workspaces/default/notes"],
    "ignore": ["*.tmp", "*.bak"],
    "max_bytes": 1000000,
    "owner_user_id": "user_primary",
    "space_id": "private_primary",
    "visibility": "private"
  }
}
```

然后启动：

```bash
./start.sh
```

`./start.sh` 会读取 `.pska/config.json`：`startup.bootstrap=true` 时准备数据库和知识源，`startup.backend=true` 时启动后端，`startup.frontend.enabled=true` 时启动前端。

`.pska/config.json` 是本机配置，不要提交。

### 最短本地流程

如果你想清空本地库，然后启动，并手动跑一次 digest：

```bash
./scripts/pska --config .pska/config.json db-reset --name pska
./start.sh
./scripts/pska --config .pska/config.json digest-now
```

`db-reset` 会删除并重建指定的本地数据库，是破坏性操作，只在你明确想要重新冷启动时使用。`digest-now` 会先执行 file sync，包括配置的 folder sources 和 workspace 的 Twitter/X archive inbox，然后调度并处理一次 digest。

### 一键启动 Workspace

```bash
./start.sh
```

它会启动 PSKA 后端 supervisor 和 React/TypeScript Workspace。打开：

```text
http://127.0.0.1:5173/
```

后端默认运行在：

```text
http://127.0.0.1:8765/
```

### Twitter/X 归档

```bash
cd channels/twitter-x
../../.pska/venvs/pska-py312/bin/python -m playwright install chromium

# 归档一条推文
archive save https://x.com/user/status/123456789
```

或使用 Chrome 扩展：

1. 打开 `chrome://extensions`，启用开发者模式
2. 加载未打包扩展：`channels/twitter-x/extension/`
3. 访问推文页面并点击扩展图标

### PSKA Core 摄入

```bash
./scripts/pska --config .pska/config.json \
  import-twitter-zips \
  --input ~/PSKA_workspaces/default/twitter_archive
```

### 搜索

```bash
./scripts/pska --config .pska/config.json \
  agentic-search --query "你的问题"
```

## 状态

| 组件 | 状态 |
|-----------|--------|
| Twitter/X 通道 | ✅ MVP 完成 |
| PSKA Core | ✅ MVP 完成 |
| Chrome 扩展 | ✅ v0.4.0 |
| LLM 提取 | ✅ 已实现 |
| 代理式搜索 | ✅ 已实现 |
| MCP 接口 | ✅ 已实现 |
| 生产 UI | 🟡 User Workspace scaffold in `frontend/` |
| 异步任务 | ✅ Durable MVP |

查看 [core/docs/mvp-status.md](core/docs/mvp-status.md) 了解详细状态。

## 许可证

MIT
