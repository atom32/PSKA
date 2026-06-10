from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

BrowserContext = Any
Page = Any


class SessionManager:
    def __init__(self, profile_root: Path, service: str = "twitter") -> None:
        self.profile_dir = profile_root / service

    @contextmanager
    def browser_context(
        self,
        *,
        headless: bool,
        timeout_ms: int,
        viewport: dict[str, int] | None = None,
    ) -> Iterator[BrowserContext]:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run `python3 -m pip install -e .` "
                "and `python3 -m playwright install chromium`."
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=headless,
                viewport=viewport or {"width": 1400, "height": 1200},
                locale="en-US",
            )
            context.set_default_timeout(timeout_ms)
            try:
                yield context
            finally:
                context.close()

    def login(self, *, timeout_ms: int) -> None:
        with self.browser_context(headless=False, timeout_ms=timeout_ms) as context:
            page = context.new_page()
            page.goto("https://x.com/login", wait_until="domcontentloaded")
            print("A Chromium window is open. Log in to Twitter/X, then press Enter here.")
            input()


def first_page(context: BrowserContext) -> Page:
    return context.pages[0] if context.pages else context.new_page()
