from __future__ import annotations

from dataclasses import dataclass
import re
from time import sleep
from typing import Any
from urllib.parse import urlparse

from pska.collectors.base import Collector
from pska.config import TwitterConfig
from pska.models import ArchiveRecord, Comment, MediaItem
from pska.session import SessionManager, first_page

Locator = Any
Page = Any


TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")


class TwitterArchiveError(RuntimeError):
    pass


def parse_tweet_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"x.com", "twitter.com", "mobile.twitter.com"}:
        raise ValueError(f"Unsupported Twitter/X host: {parsed.netloc}")
    match = TWEET_ID_RE.search(parsed.path)
    if not match:
        raise ValueError(f"Could not find tweet id in URL: {url}")
    return match.group(1)


def normalize_twitter_url(url: str) -> str:
    tweet_id = parse_tweet_id(url)
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    author = parts[0] if parts else "i"
    return f"https://x.com/{author}/status/{tweet_id}"


@dataclass(slots=True)
class TwitterCollector(Collector):
    session: SessionManager
    config: TwitterConfig

    def collect(self, url: str) -> ArchiveRecord:
        normalized_url = normalize_twitter_url(url)
        tweet_id = parse_tweet_id(normalized_url)
        last_error: Exception | None = None
        for attempt in range(1, self.config.retry + 1):
            try:
                return self._collect_once(normalized_url, tweet_id)
            except Exception as exc:  # noqa: BLE001 - preserve retry cause for CLI.
                last_error = exc
                if attempt < self.config.retry:
                    sleep(min(attempt * 2, 8))
        raise TwitterArchiveError(f"Failed to archive {normalized_url}: {last_error}") from last_error

    def _collect_once(self, url: str, tweet_id: str) -> ArchiveRecord:
        with self.session.browser_context(
            headless=self.config.headless,
            timeout_ms=self.config.timeout_ms,
        ) as context:
            page = first_page(context)
            page.goto(url, wait_until="domcontentloaded")
            self._wait_for_tweet(page)
            self._expand_visible_text(page)
            self._scroll_for_replies(page)
            articles = page.locator("article").all()
            if not articles:
                raise TwitterArchiveError("No tweet articles found. Login may be required.")

            main_article = self._find_main_article(articles, tweet_id)
            replies = self._extract_replies(articles, main_article, tweet_id)
            record = self._extract_main_record(main_article, tweet_id, url)
            record.replies = replies[: self.config.max_comments]
            record.raw_html = page.content()
            record.screenshot = page.screenshot(full_page=True)
            return record

    def _wait_for_tweet(self, page: Page) -> None:
        try:
            page.wait_for_selector("article", timeout=self.config.timeout_ms)
        except Exception as exc:  # noqa: BLE001 - Playwright timeout type is optional at import time.
            raise TwitterArchiveError("Timed out waiting for tweet content.") from exc

    def _expand_visible_text(self, page: Page) -> None:
        for label in ("Show more", "显示更多", "查看更多", "More"):
            try:
                page.get_by_text(label, exact=True).click(timeout=1500)
            except Exception:  # noqa: BLE001 - best effort against localized UI.
                continue

    def _scroll_for_replies(self, page: Page) -> None:
        for _ in range(self.config.max_scrolls):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(self.config.scroll_delay_ms)

    def _find_main_article(self, articles: list[Locator], tweet_id: str) -> Locator:
        for article in articles:
            hrefs = self._hrefs(article)
            if any(f"/status/{tweet_id}" in href or f"/statuses/{tweet_id}" in href for href in hrefs):
                return article
        return articles[0]

    def _extract_main_record(self, article: Locator, tweet_id: str, url: str) -> ArchiveRecord:
        author, handle = self._extract_author(article)
        images = [MediaItem(url=image_url, kind="image") for image_url in self._image_urls(article)]
        videos = [MediaItem(url=video_url, kind="video") for video_url in self._video_urls(article)]
        return ArchiveRecord(
            id=tweet_id,
            url=url,
            author=author,
            handle=handle,
            content=self._tweet_text(article),
            created_at=self._created_at(article),
            images=images,
            videos=videos,
            quoted_tweet=self._quoted_tweet(article),
        )

    def _extract_replies(
        self,
        articles: list[Locator],
        main_article: Locator,
        main_tweet_id: str,
    ) -> list[Comment]:
        replies: list[Comment] = []
        seen: set[str] = set()
        for article in articles:
            if article == main_article:
                continue
            hrefs = self._hrefs(article)
            status_url = next((href for href in hrefs if "/status/" in href), None)
            comment_id = self._status_id(status_url) if status_url else None
            if comment_id == main_tweet_id:
                continue
            dedupe_key = comment_id or self._safe_inner_text(article)
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            author, handle = self._extract_author(article)
            text = self._tweet_text(article)
            if not text:
                continue
            replies.append(
                Comment(
                    id=comment_id,
                    url=self._absolute_url(status_url) if status_url else None,
                    author=author,
                    handle=handle,
                    text=text,
                    created_at=self._created_at(article),
                    images=self._image_urls(article),
                    videos=self._video_urls(article),
                    raw_text=self._safe_inner_text(article),
                )
            )
            if len(replies) >= self.config.max_comments:
                break
        return replies

    def _tweet_text(self, article: Locator) -> str:
        texts = []
        for text_node in article.locator('[data-testid="tweetText"]').all():
            text = self._safe_inner_text(text_node).strip()
            if text:
                texts.append(text)
        if texts:
            return "\n\n".join(dict.fromkeys(texts))
        return self._safe_inner_text(article).strip()

    def _extract_author(self, article: Locator) -> tuple[str | None, str | None]:
        raw = self._safe_inner_text(article)
        handle_match = re.search(r"@[\w_]+", raw)
        handle = handle_match.group(0) if handle_match else None
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        author = None
        if handle:
            for index, line in enumerate(lines):
                if line == handle and index > 0:
                    author = lines[index - 1]
                    break
        return author, handle

    def _created_at(self, article: Locator) -> str | None:
        try:
            value = article.locator("time").first.get_attribute("datetime", timeout=1000)
            return value
        except Exception:  # noqa: BLE001
            return None

    def _hrefs(self, article: Locator) -> list[str]:
        hrefs: list[str] = []
        for link in article.locator("a[href]").all():
            href = link.get_attribute("href")
            if href:
                hrefs.append(href)
        return hrefs

    def _image_urls(self, article: Locator) -> list[str]:
        urls: list[str] = []
        for image in article.locator("img[src]").all():
            src = image.get_attribute("src")
            if src and ("twimg.com/media" in src or "pbs.twimg.com/media" in src):
                urls.append(src)
        return list(dict.fromkeys(urls))

    def _video_urls(self, article: Locator) -> list[str]:
        urls: list[str] = []
        for video in article.locator("video[src], source[src]").all():
            src = video.get_attribute("src")
            if src:
                urls.append(src)
        for href in self._hrefs(article):
            if any(marker in href for marker in ("/i/status/", "/video/", ".m3u8", ".mp4")):
                urls.append(self._absolute_url(href))
        return list(dict.fromkeys(urls))

    def _quoted_tweet(self, article: Locator) -> dict[str, str] | None:
        quoted = article.locator('[role="link"]').filter(has_text=re.compile(r"@")).all()
        for item in quoted[1:]:
            text = self._safe_inner_text(item).strip()
            href = item.get_attribute("href")
            if text and href and "/status/" in href:
                return {"url": self._absolute_url(href), "text": text}
        return None

    def _safe_inner_text(self, locator: Locator) -> str:
        try:
            return locator.inner_text(timeout=1500)
        except Exception:  # noqa: BLE001
            return ""

    def _status_id(self, href: str | None) -> str | None:
        if not href:
            return None
        match = TWEET_ID_RE.search(href)
        return match.group(1) if match else None

    def _absolute_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return f"https://x.com{href}"
