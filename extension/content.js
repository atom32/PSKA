(() => {
  const STATUS_RE = /\/status(?:es)?\/(\d+)/;

  function parseTweetId(url) {
    const match = String(url || "").match(STATUS_RE);
    return match ? match[1] : null;
  }

  function absoluteUrl(href) {
    if (!href) return null;
    if (href.startsWith("http")) return href;
    return new URL(href, location.origin).href;
  }

  function unique(values) {
    return Array.from(new Set(values.filter(Boolean)));
  }

  function originalImageUrl(src) {
    try {
      const url = new URL(src);
      if (url.hostname.endsWith("twimg.com") && url.pathname.includes("/media/")) {
        if (!url.searchParams.get("format")) {
          const match = url.pathname.match(/\.(jpg|jpeg|png|webp)$/i);
          url.searchParams.set("format", match ? match[1].toLowerCase() : "jpg");
        }
        url.searchParams.set("name", "orig");
        return url.toString();
      }
    } catch (_error) {
      return src;
    }
    return src;
  }

  function textOf(node) {
    return node ? node.innerText.trim() : "";
  }

  function hrefs(article) {
    return Array.from(article.querySelectorAll("a[href]")).map((a) => a.getAttribute("href"));
  }

  function findMainArticle(tweetId) {
    const articles = Array.from(document.querySelectorAll("article"));
    return (
      articles.find((article) =>
        hrefs(article).some((href) => href && (href.includes(`/status/${tweetId}`) || href.includes(`/statuses/${tweetId}`)))
      ) || articles[0] || null
    );
  }

  function tweetText(article) {
    const nodes = Array.from(article.querySelectorAll('[data-testid="tweetText"]'));
    const parts = unique(nodes.map(textOf));
    return parts.length ? parts.join("\n\n") : textOf(article);
  }

  function authorAndHandle(article) {
    const raw = textOf(article);
    const lines = raw.split("\n").map((line) => line.trim()).filter(Boolean);
    const handle = (raw.match(/@[\w_]+/) || [null])[0];
    let author = null;
    if (handle) {
      const handleIndex = lines.findIndex((line) => line === handle);
      if (handleIndex > 0) author = lines[handleIndex - 1];
    }
    return { author, handle };
  }

  function createdAt(article) {
    return article.querySelector("time")?.getAttribute("datetime") || null;
  }

  function imageUrls(article) {
    return unique(
      Array.from(article.querySelectorAll("img[src]"))
        .map((img) => img.getAttribute("src"))
        .filter((src) => src && (src.includes("twimg.com/media") || src.includes("pbs.twimg.com/media")))
        .map(originalImageUrl)
    );
  }

  function videoUrls(article) {
    const media = Array.from(article.querySelectorAll("video[src], source[src]")).map((node) => node.getAttribute("src"));
    const links = hrefs(article)
      .filter((href) => href && (href.includes("/video/") || href.includes(".m3u8") || href.includes(".mp4")))
      .map(absoluteUrl);
    return unique([...media, ...links]);
  }

  function statusUrl(article) {
    const href = hrefs(article).find((item) => item && STATUS_RE.test(item));
    return href ? absoluteUrl(href) : null;
  }

  function statusId(article) {
    const url = statusUrl(article);
    return parseTweetId(url);
  }

  function extractComment(article, mainTweetId) {
    const id = statusId(article);
    if (id === mainTweetId) return null;
    const text = tweetText(article);
    if (!text) return null;
    const { author, handle } = authorAndHandle(article);
    return {
      id,
      url: statusUrl(article),
      author,
      handle,
      text,
      created_at: createdAt(article),
      images: imageUrls(article),
      videos: videoUrls(article),
      metrics: {},
      raw_text: textOf(article),
      source: "visible_dom"
    };
  }

  function extractTweet() {
    const tweetId = parseTweetId(location.href);
    if (!tweetId) throw new Error("Current page is not a Twitter/X status URL.");

    const mainArticle = findMainArticle(tweetId);
    if (!mainArticle) throw new Error("No tweet article found on the page.");

    const { author, handle } = authorAndHandle(mainArticle);
    const comments = [];
    const seen = new Set();
    for (const article of Array.from(document.querySelectorAll("article"))) {
      if (article === mainArticle) continue;
      const comment = extractComment(article, tweetId);
      if (!comment) continue;
      const key = comment.id || comment.raw_text;
      if (seen.has(key)) continue;
      seen.add(key);
      comments.push(comment);
      if (comments.length >= 50) break;
    }

    const images = imageUrls(mainArticle);
    const videos = videoUrls(mainArticle);
    return {
      id: tweetId,
      url: location.href,
      author,
      handle,
      content: tweetText(mainArticle),
      created_at: createdAt(mainArticle),
      images,
      videos,
      replies: comments,
      raw_html: `<!doctype html>\n${document.documentElement.outerHTML}`,
      captured_at: new Date().toISOString()
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "PSKA_EXTRACT_TWEET") return false;
    try {
      sendResponse({ ok: true, record: extractTweet() });
    } catch (error) {
      sendResponse({ ok: false, error: error.message || String(error) });
    }
    return false;
  });
})();
