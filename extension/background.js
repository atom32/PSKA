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

function dataUrlFromBytes(bytes, mimeType) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return `data:${mimeType};base64,${btoa(binary)}`;
}

function bytesFromDataUrl(dataUrl) {
  const [, metadata = "", payload = ""] = dataUrl.match(/^data:([^,]*),(.*)$/) || [];
  if (!metadata.includes(";base64")) return new TextEncoder().encode(decodeURIComponent(payload));
  const binary = atob(payload);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function extensionForUrl(url, fallback) {
  try {
    const parsed = new URL(url);
    const format = parsed.searchParams.get("format");
    if (format && /^[a-z0-9]+$/i.test(format)) return `.${format.toLowerCase()}`;
    const path = parsed.pathname;
    const match = path.match(/\.(jpg|jpeg|png|webp|gif|mp4|m3u8)$/i);
    return match ? `.${match[1].toLowerCase()}` : fallback;
  } catch (_error) {
    return fallback;
  }
}

function markdown(record) {
  const lines = [
    record.kind === "x_article" ? "# X Article" : "# Tweet",
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

async function downloadUrl(filename, url) {
  return chrome.downloads.download({
    url,
    filename,
    conflictAction: "overwrite",
    saveAs: false
  });
}

async function fetchRemoteBytes(url, fallbackMimeType = "application/octet-stream") {
  const response = await fetch(url, { credentials: "omit", cache: "force-cache" });
  if (!response.ok) throw new Error(`HTTP ${response.status} while downloading ${url}`);
  return {
    bytes: new Uint8Array(await response.arrayBuffer()),
    contentType: response.headers.get("content-type") || fallbackMimeType
  };
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function writeUint16(bytes, offset, value) {
  bytes[offset] = value & 0xff;
  bytes[offset + 1] = (value >>> 8) & 0xff;
}

function writeUint32(bytes, offset, value) {
  bytes[offset] = value & 0xff;
  bytes[offset + 1] = (value >>> 8) & 0xff;
  bytes[offset + 2] = (value >>> 16) & 0xff;
  bytes[offset + 3] = (value >>> 24) & 0xff;
}

function concatBytes(parts) {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const result = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

function zipStore(files) {
  const encoder = new TextEncoder();
  const localParts = [];
  const centralParts = [];
  let offset = 0;

  for (const file of files) {
    const pathBytes = encoder.encode(file.path);
    const data = file.bytes;
    const checksum = crc32(data);

    const local = new Uint8Array(30 + pathBytes.length);
    writeUint32(local, 0, 0x04034b50);
    writeUint16(local, 4, 20);
    writeUint16(local, 6, 0x0800);
    writeUint16(local, 8, 0);
    writeUint16(local, 10, 0);
    writeUint16(local, 12, 0);
    writeUint32(local, 14, checksum);
    writeUint32(local, 18, data.length);
    writeUint32(local, 22, data.length);
    writeUint16(local, 26, pathBytes.length);
    writeUint16(local, 28, 0);
    local.set(pathBytes, 30);
    localParts.push(local, data);

    const central = new Uint8Array(46 + pathBytes.length);
    writeUint32(central, 0, 0x02014b50);
    writeUint16(central, 4, 20);
    writeUint16(central, 6, 20);
    writeUint16(central, 8, 0x0800);
    writeUint16(central, 10, 0);
    writeUint16(central, 12, 0);
    writeUint16(central, 14, 0);
    writeUint32(central, 16, checksum);
    writeUint32(central, 20, data.length);
    writeUint32(central, 24, data.length);
    writeUint16(central, 28, pathBytes.length);
    writeUint16(central, 30, 0);
    writeUint16(central, 32, 0);
    writeUint16(central, 34, 0);
    writeUint16(central, 36, 0);
    writeUint32(central, 38, 0);
    writeUint32(central, 42, offset);
    central.set(pathBytes, 46);
    centralParts.push(central);

    offset += local.length + data.length;
  }

  const centralDirectory = concatBytes(centralParts);
  const end = new Uint8Array(22);
  writeUint32(end, 0, 0x06054b50);
  writeUint16(end, 8, files.length);
  writeUint16(end, 10, files.length);
  writeUint32(end, 12, centralDirectory.length);
  writeUint32(end, 16, offset);
  writeUint16(end, 20, 0);

  return concatBytes([...localParts, centralDirectory, end]);
}

function textFile(path, text) {
  return { path, bytes: new TextEncoder().encode(text) };
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
  const tweetId = sanitizeFilenamePart(record.id);
  const base = `${tweetId}`;
  const files = [
    textFile(`${base}/raw.html`, record.raw_html || ""),
    { path: `${base}/screenshot.png`, bytes: bytesFromDataUrl(screenshotUrl) },
    textFile(`${base}/content.md`, markdown(record)),
    textFile(`${base}/comments.json`, JSON.stringify(record.replies || [], null, 2)),
    textFile(`${base}/metadata.json`, JSON.stringify({ ...record, raw_html: undefined, replies: undefined }, null, 2))
  ];

  for (const [index, url] of (record.images || []).entries()) {
    const ext = extensionForUrl(url, ".jpg");
    const mediaPath = `${base}/media/image_${String(index + 1).padStart(2, "0")}${ext}`;
    try {
      const image = await fetchRemoteBytes(url, "image/jpeg");
      files.push({ path: mediaPath, bytes: image.bytes });
    } catch (error) {
      files.push(textFile(
        `${base}/media/image_${String(index + 1).padStart(2, "0")}_download_error.txt`,
        `Failed to download image into archive folder.\nURL: ${url}\nError: ${error.message || String(error)}\n`,
      ));
    }
  }

  const zipBytes = zipStore(files);
  await downloadUrl(`twitter_archive/${tweetId}.zip`, dataUrlFromBytes(zipBytes, "application/zip"));

  if (!options.skipDelay) await sleep(300);
  return { id: record.id, url: record.url, path: `twitter_archive/${tweetId}.zip` };
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

  await downloadUrl(
    "twitter_archive/batch_report.zip",
    dataUrlFromBytes(
      zipStore([textFile("batch_report.json", JSON.stringify(report, null, 2))]),
      "application/zip"
    )
  );
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
