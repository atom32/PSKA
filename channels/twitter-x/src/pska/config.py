from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


DEFAULT_CONFIG = """# PSKA configuration
[twitter]
max_scrolls = 5
scroll_delay_ms = 1200
max_comments = 50
timeout_ms = 60000
retry = 3
headless = true

[storage]
archive_dir = "archive"
profile_dir = ".pska/profiles"
"""


@dataclass(frozen=True, slots=True)
class TwitterConfig:
    max_scrolls: int = 5
    scroll_delay_ms: int = 1200
    max_comments: int = 50
    timeout_ms: int = 60000
    retry: int = 3
    headless: bool = True


@dataclass(frozen=True, slots=True)
class StorageConfig:
    archive_dir: Path = Path("archive")
    profile_dir: Path = Path(".pska/profiles")


@dataclass(frozen=True, slots=True)
class AppConfig:
    twitter: TwitterConfig
    storage: StorageConfig


def ensure_config(path: Path = Path(".pska/config.toml")) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return path


def load_config(path: Path = Path(".pska/config.toml")) -> AppConfig:
    ensure_config(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    twitter_data = data.get("twitter", {})
    storage_data = data.get("storage", {})
    return AppConfig(
        twitter=TwitterConfig(
            max_scrolls=int(twitter_data.get("max_scrolls", 5)),
            scroll_delay_ms=int(twitter_data.get("scroll_delay_ms", 1200)),
            max_comments=int(twitter_data.get("max_comments", 50)),
            timeout_ms=int(twitter_data.get("timeout_ms", 60000)),
            retry=int(twitter_data.get("retry", 3)),
            headless=bool(twitter_data.get("headless", True)),
        ),
        storage=StorageConfig(
            archive_dir=Path(storage_data.get("archive_dir", "archive")),
            profile_dir=Path(storage_data.get("profile_dir", ".pska/profiles")),
        ),
    )
