const MAX_BATCH_ITEMS = 100;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseTweetId(url) {
  const match = String(url || "").match(/\/status(?:es)?\/(\d+)/);
  return match ? match[1] : null;
}

function isTweetUrl(url) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.replace(/^www\./, "");
    return ["x.com", "twitter.com", "mobile.twitter.com"].includes(host) && Boolean(parseTweetId(url));
  } catch (_error) {
    return false;
  }
}

function sanitizeFilenamePart(value) {
  return String(value || "unknown").replace(/[\\/:*?"<>|]+/g, "_").slice(0, 120);
}

function dataUrlFromText(text, mimeType) {
  const encoded = new TextEncoder().encode(text);
  return dataUrlFromBytes(encoded, mimeType);
}

function dataUrlFromBytes(bytes, mimeType) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return `data:${mimeType};base64,${btoa(binary)}`;
}

function extensionForUrl(url, fallback) {
  try {
    const path = new URL(url).pathname;
    const match = path.match(/\.(jpg|jpeg|png|webp|gif|mp4|m3u8)$/i);
    return match ? `.${match[1].toLowerCase()}` : fallback;
  } catch (_error) {
    return fallback;
  }
}

function markdown(record) {
  const lines = [
    "# Tweet",
    "",
    `ID: ${record.id}`,
    `Author: ${record.author || ""}`,
    `Handle: ${record.handle || ""}`,
    `Date: ${record.created_at || ""}`,
    "",
    "URL:",
    record.url,
    "",
    "---",
    "",
    (record.content || "").trim(),
    "",
    "---",
    "",
    "## Media",
    ""
  ];

  if (!record.images.length && !record.videos.length) lines.push("No media captured.");
  record.images.forEach((url, index) => lines.push(`- Image ${index + 1}: media/image_${String(index + 1).padStart(2, "0")}${extensionForUrl(url, ".jpg")}`));
  record.videos.forEach((url, index) => lines.push(`- Video ${index + 1}: ${url}`));

  lines.push("", "---", "", "## Top Replies", "");
  if (!record.replies.length) lines.push("No replies captured.");
  record.replies.forEach((reply) => {
    lines.push(
      `### ${reply.author || reply.handle || reply.id || "Reply"}`,
      "",
      `Handle: ${reply.handle || ""}`,
      `Date: ${reply.created_at || ""}`,
      `URL: ${reply.url || ""}`,
      "",
      (reply.text || "").trim(),
      ""
    );
  });

  return `${lines.join("\n").trim()}\n`;
}

async function downloadText(filename, text, mimeType) {
  return chrome.downloads.download({
    url: dataUrlFromText(text, mimeType),
    filename,
    conflictAction: "overwrite",
    saveAs: false
  });
}

async function downloadUrl(filename, url) {
  return chrome.downloads.download({
    url,
    filename,
    conflictAction: "overwrite",
    saveAs: false
  });
}

async function downloadRemoteAsFile(filename, url, fallbackMimeType = "application/octet-stream") {
  const response = await fetch(url, { credentials: "omit", cache: "force-cache" });
  if (!response.ok) throw new Error(`HTTP ${response.status} while downloading ${url}`);
  const contentType = response.headers.get("content-type") || fallbackMimeType;
  const bytes = new Uint8Array(await response.arrayBuffer());
  return downloadUrl(filename, dataUrlFromBytes(bytes, contentType));
}

async function extractFromTab(tabId) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: "PSKA_EXTRACT_TWEET" });
    if (!response?.ok) throw new Error(response?.error || "Content extraction failed.");
    return response.record;
  } catch (_error) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    const response = await chrome.tabs.sendMessage(tabId, { type: "PSKA_EXTRACT_TWEET" });
    if (!response?.ok) throw new Error(response?.error || "Content extraction failed.");
    return response.record;
  }
}

async function archiveTab(tab, options = {}) {
  if (!tab?.id || !isTweetUrl(tab.url)) throw new Error("Open a Twitter/X status URL first.");

  const record = await extractFromTab(tab.id);
  const screenshotUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  const base = `twitter_archive/${sanitizeFilenamePart(record.id)}`;

  await downloadText(`${base}/raw.html`, record.raw_html || "", "text/html;charset=utf-8");
  await downloadUrl(`${base}/screenshot.png`, screenshotUrl);
  await downloadText(`${base}/content.md`, markdown(record), "text/markdown;charset=utf-8");
  await downloadText(`${base}/comments.json`, JSON.stringify(record.replies || [], null, 2), "application/json;charset=utf-8");
  await downloadText(`${base}/metadata.json`, JSON.stringify({ ...record, raw_html: undefined, replies: undefined }, null, 2), "application/json;charset=utf-8");

  for (const [index, url] of (record.images || []).entries()) {
    const ext = extensionForUrl(url, ".jpg");
    const mediaPath = `${base}/media/image_${String(index + 1).padStart(2, "0")}${ext}`;
    try {
      await downloadRemoteAsFile(mediaPath, url, "image/jpeg");
    } catch (error) {
      await downloadText(
        `${base}/media/image_${String(index + 1).padStart(2, "0")}_download_error.txt`,
        `Failed to download image into archive folder.\nURL: ${url}\nError: ${error.message || String(error)}\n`,
        "text/plain;charset=utf-8"
      );
    }
  }

  if (!options.skipDelay) await sleep(300);
  return { id: record.id, url: record.url, path: base };
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function waitForTabComplete(tabId) {
  for (let i = 0; i < 120; i += 1) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tab;
    await sleep(500);
  }
  throw new Error("Timed out waiting for tab load.");
}

async function archiveBatch(urls) {
  const cleanUrls = urls.map((url) => url.trim()).filter(Boolean).slice(0, MAX_BATCH_ITEMS);
  const report = { success: [], failed: [] };

  for (const url of cleanUrls) {
    if (!isTweetUrl(url)) {
      report.failed.push({ url, error: "Not a supported Twitter/X status URL." });
      continue;
    }

    let tab = null;
    try {
      tab = await chrome.tabs.create({ url, active: true });
      await waitForTabComplete(tab.id);
      await sleep(2500);
      const result = await archiveTab(tab, { skipDelay: true });
      report.success.push(result);
    } catch (error) {
      report.failed.push({ url, error: error.message || String(error) });
    } finally {
      if (tab?.id) {
        try {
          await chrome.tabs.remove(tab.id);
        } catch (_error) {
          // The user may have already closed it.
        }
      }
    }
  }

  await downloadText("twitter_archive/batch_report.json", JSON.stringify(report, null, 2), "application/json;charset=utf-8");
  return report;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message?.type === "PSKA_ARCHIVE_CURRENT") {
      return archiveTab(await activeTab());
    }
    if (message?.type === "PSKA_ARCHIVE_BATCH") {
      return archiveBatch(message.urls || []);
    }
    throw new Error(`Unknown message type: ${message?.type}`);
  })()
    .then((result) => sendResponse({ ok: true, result }))
    .catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});
