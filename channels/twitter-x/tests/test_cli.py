from __future__ import annotations

from pathlib import Path

import pska.cli
from pska.cli import build_parser
from pska.config import AppConfig, StorageConfig, TwitterConfig


def test_cli_accepts_batch_file() -> None:
    args = build_parser().parse_args(["batch", "urls.txt"])
    assert args.command == "batch"
    assert args.urls_file == Path("urls.txt")


def test_batch_continues_after_failure(tmp_path, monkeypatch) -> None:
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "https://x.com/alice/status/1\nhttps://x.com/bob/status/2\n",
        encoding="utf-8",
    )
    config = AppConfig(
        twitter=TwitterConfig(),
        storage=StorageConfig(archive_dir=tmp_path / "archive", profile_dir=tmp_path / "profiles"),
    )

    calls = []

    def fake_save(url, config, *, headed, report):
        calls.append(url)
        if url.endswith("/1"):
            report["failed"].append({"url": url, "error": "boom"})
            return 1
        report["success"].append({"url": url, "tweet_id": "2", "path": "archive/twitter/2"})
        return 0

    monkeypatch.setattr(pska.cli, "_save_for_batch", fake_save)

    assert pska.cli.batch(urls_file, config) == 1
    assert calls == ["https://x.com/alice/status/1", "https://x.com/bob/status/2"]
    report = (tmp_path / "archive" / "batch_report.json").read_text(encoding="utf-8")
    assert "boom" in report
    assert '"success"' in report
