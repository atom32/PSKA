from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from pska.archive import ArchiveBuilder
from pska.collectors.twitter import TwitterArchiveError, TwitterCollector
from pska.config import AppConfig, load_config
from pska.session import SessionManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archive", description="Personal Social Knowledge Archive CLI")
    parser.add_argument("--config", default=".pska/config.toml", help="Path to config TOML")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Create or refresh a service login session")
    login_parser.add_argument("service", choices=["twitter"])

    save_parser = subparsers.add_parser("save", help="Archive one URL")
    save_parser.add_argument("url")
    save_parser.add_argument("--headed", action="store_true", help="Run browser in headed mode for debugging")

    batch_parser = subparsers.add_parser("batch", help="Archive URLs from a text file")
    batch_parser.add_argument("urls_file", type=Path)
    batch_parser.add_argument("--headed", action="store_true", help="Run browser in headed mode for debugging")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(Path(args.config))

    if args.command == "login":
        return login(args.service, config)
    if args.command == "save":
        return save(args.url, config, headed=args.headed)
    if args.command == "batch":
        return batch(args.urls_file, config, headed=args.headed)
    parser.error(f"Unknown command: {args.command}")
    return 2


def login(service: str, config: AppConfig) -> int:
    if service != "twitter":
        raise ValueError(f"Unsupported service: {service}")
    session = SessionManager(config.storage.profile_dir, service="twitter")
    session.login(timeout_ms=config.twitter.timeout_ms)
    print("Login session saved.")
    return 0


def save(url: str, config: AppConfig, *, headed: bool = False) -> int:
    twitter_config = config.twitter
    if headed:
        twitter_config = type(twitter_config)(
            max_scrolls=twitter_config.max_scrolls,
            scroll_delay_ms=twitter_config.scroll_delay_ms,
            max_comments=twitter_config.max_comments,
            timeout_ms=twitter_config.timeout_ms,
            retry=twitter_config.retry,
            headless=False,
        )
    collector = TwitterCollector(SessionManager(config.storage.profile_dir, service="twitter"), twitter_config)
    builder = ArchiveBuilder(config.storage.archive_dir)
    try:
        record = collector.collect(url)
        tweet_dir = builder.write(record)
    except (TwitterArchiveError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Archived {record.url} -> {tweet_dir}")
    return 0


def batch(urls_file: Path, config: AppConfig, *, headed: bool = False) -> int:
    urls = [
        line.strip()
        for line in urls_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    report: dict[str, list[dict[str, str]]] = {"success": [], "failed": []}
    exit_code = 0
    for url in urls:
        code = _save_for_batch(url, config, headed=headed, report=report)
        if code != 0:
            exit_code = 1
    config.storage.archive_dir.mkdir(parents=True, exist_ok=True)
    report_path = config.storage.archive_dir / "batch_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Batch report written to {report_path}")
    return exit_code


def _save_for_batch(
    url: str,
    config: AppConfig,
    *,
    headed: bool,
    report: dict[str, list[dict[str, str]]],
) -> int:
    twitter_config = config.twitter
    if headed:
        twitter_config = type(twitter_config)(
            max_scrolls=twitter_config.max_scrolls,
            scroll_delay_ms=twitter_config.scroll_delay_ms,
            max_comments=twitter_config.max_comments,
            timeout_ms=twitter_config.timeout_ms,
            retry=twitter_config.retry,
            headless=False,
        )
    collector = TwitterCollector(SessionManager(config.storage.profile_dir, service="twitter"), twitter_config)
    builder = ArchiveBuilder(config.storage.archive_dir)
    try:
        record = collector.collect(url)
        tweet_dir = builder.write(record)
    except (TwitterArchiveError, ValueError) as exc:
        report["failed"].append({"url": url, "error": str(exc)})
        print(f"FAILED {url}: {exc}", file=sys.stderr)
        return 1
    report["success"].append({"url": url, "tweet_id": record.id, "path": str(tweet_dir)})
    print(f"OK {url} -> {tweet_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
