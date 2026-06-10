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

### Twitter/X 归档

```bash
cd channels/twitter-x
python3 -m pip install -e .
python3 -m playwright install chromium

# 归档一条推文
archive save https://x.com/user/status/123456789
```

或使用 Chrome 扩展：

1. 打开 `chrome://extensions`，启用开发者模式
2. 加载未打包扩展：`channels/twitter-x/extension/`
3. 访问推文页面并点击扩展图标

### PSKA Core 摄入

```bash
cd core
PYTHONPATH=src python3 -m pska_core.cli db-reset --name pska_smoke
PYTHONPATH=src python3 -m pska_core.cli \
  --database-url postgresql:///pska_smoke \
  import-twitter-zips \
  --input ~/Downloads/twitter_archive
```

### 搜索

```bash
PYTHONPATH=src python3 -m pska_core.cli \
  --database-url postgresql:///pska_smoke \
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
| 生产 UI | ❌ 未开始 |
| 异步任务 | ❌ 未开始 |

查看 [core/docs/mvp-status.md](core/docs/mvp-status.md) 了解详细状态。

## 许可证

MIT
