# PSKA Twitter/X 通道

PSKA 的 Twitter/X 采集通道。

归档使用 `docs/schema.md` 中记录的 PSKA v1 元数据 schema。

## 安装

从仓库根目录：

```bash
cd channels/twitter-x
```

```bash
python3 -m pip install -e .
python3 -m playwright install chromium
```

如果 `uv` 可用：

```bash
uv sync
uv run playwright install chromium
```

## 使用方法

```bash
archive login twitter
archive save https://x.com/user/status/123456789
archive batch urls.txt
```

等效的模块形式：

```bash
PYTHONPATH=src python3 -m pska.cli save https://x.com/user/status/123456789
```

归档写入到：

```text
archive/twitter/<tweet_id>/
  raw.html
  screenshot.png
  content.md
  comments.json
  metadata.json
  media/
```

首次运行时会在 `.pska/config.toml` 创建配置文件。

## Chrome 扩展

如果 Twitter/X 限制 Playwright 登录，请使用 `extension/` 中的未打包 Chrome 扩展。
它在你已登录的 Chrome 会话中运行。

1. 打开 `chrome://extensions`
2. 启用开发者模式
3. 点击"加载已解压的扩展程序"
4. 从仓库根目录选择此文件夹：

```text
channels/twitter-x/extension/
```

如果你已经在 `channels/twitter-x` 目录中，则选择 `extension/`。

使用方法：

1. 在 Chrome 中打开一条推文/X 状态页面
2. 点击 PSKA Archive 扩展
3. 点击"Archive current Tweet"，或在"Batch URLs"中粘贴多个 URL

Chrome 会在以下位置为每条推文下载一个 ZIP：

```text
Downloads/twitter_archive/<tweet_id>.zip
```

ZIP 包含：

```text
<tweet_id>/
  raw.html
  screenshot.png
  content.md
  comments.json
  metadata.json
  media/
```

批处理模式还会写入 `Downloads/twitter_archive/batch_report.zip`。

扩展在回到页面顶部后捕获可见标签页截图。它不会绕过 Twitter/X 的媒体限制；视频在 Markdown/JSON 中保存为链接。
