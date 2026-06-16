from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pska_core.serde import dumps
from pska_core.store_postgres import PostgresKnowledgeStore

DEFAULT_DATABASE_NAME = "pska_mvp_plus_sample"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_real_sample_smoke(args)
    print(dumps(report))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run PSKA MVP+ smoke against a small real Twitter/X archive sample.")
    parser.add_argument("--input", type=Path, default=Path.home() / "Downloads" / "twitter_archive")
    parser.add_argument("--archive-root", type=Path, default=ROOT / "archive" / "mvp_plus_sample")
    parser.add_argument("--database-name", default=DEFAULT_DATABASE_NAME)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--query", default="")
    parser.add_argument("--owner-user-id", default="user_primary")
    parser.add_argument("--skip-llm", action="store_true", help="Skip extract-all and agentic-search; useful for DB/import-only checks.")
    parser.add_argument("--python", default=sys.executable)
    return parser


def run_real_sample_smoke(args: argparse.Namespace) -> dict[str, Any]:
    sample_zips = select_sample_zips(args.input, limit=args.limit)
    if not sample_zips:
        return {
            "ok": False,
            "error": f"No Twitter/X archive zip files found in {args.input}",
            "checks": {"sample_zips_found": False},
        }

    env = _smoke_env()
    database_url = f"postgresql:///{args.database_name}"
    report: dict[str, Any] = {
        "ok": False,
        "database_url": database_url,
        "input": str(args.input),
        "sample_zips": [str(path) for path in sample_zips],
        "steps": {},
    }

    with tempfile.TemporaryDirectory(prefix="pska-mvp-plus-zips-") as temp_dir_name:
        sample_dir = Path(temp_dir_name)
        materialized = materialize_sample_zips(sample_zips, sample_dir)
        commands = [
            (
                "db_reset",
                [
                    args.python,
                    "-m",
                    "pska_core.cli",
                    "db-reset",
                    "--name",
                    args.database_name,
                ],
            ),
            (
                "import_twitter_zips",
                [
                    args.python,
                    "-m",
                    "pska_core.cli",
                    "--database-url",
                    database_url,
                    "import-twitter-zips",
                    "--input",
                    str(sample_dir),
                    "--archive-root",
                    str(args.archive_root),
                    "--owner-user-id",
                    args.owner_user_id,
                ],
            ),
        ]
        if not args.skip_llm:
            commands.append(
                (
                    "extract_all",
                    [
                        args.python,
                        "-m",
                        "pska_core.cli",
                        "--database-url",
                        database_url,
                        "extract-all",
                        "--owner-user-id",
                        args.owner_user_id,
                    ],
                )
            )

        for label, command in commands:
            result = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            report["steps"][label] = _step_result(result)
            if result.returncode != 0:
                report["materialized_sample_zips"] = [str(path) for path in materialized]
                report["checks"] = _checks_from_store(database_url, args.owner_user_id, skip_llm=args.skip_llm)
                return report

        query = args.query or sample_query(database_url)
        report["query"] = query
        for label, command in _query_commands(args, database_url, query):
            result = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            report["steps"][label] = _step_result(result)
            if result.returncode != 0:
                report["checks"] = _checks_from_store(database_url, args.owner_user_id, skip_llm=args.skip_llm)
                return report

        report["steps"]["digest_schedule"] = _step_result(
            subprocess.run(
                [
                    args.python,
                    "-m",
                    "pska_core.cli",
                    "--database-url",
                    database_url,
                    "digest-schedule",
                    "--owner-user-id",
                    args.owner_user_id,
                    "--limit",
                    str(max(1, args.limit)),
                    "--batch-size",
                    str(max(1, args.limit)),
                    "--force",
                    "--reason",
                    "mvp plus real sample smoke",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )

    checks = _checks_from_store(database_url, args.owner_user_id, skip_llm=args.skip_llm)
    report["checks"] = checks
    report["ok"] = all(checks.values()) and all(step["returncode"] == 0 for step in report["steps"].values())
    return report


def select_sample_zips(input_dir: Path, *, limit: int) -> list[Path]:
    if limit < 1:
        return []
    return sorted(path for path in input_dir.expanduser().glob("*.zip") if path.is_file())[:limit]


def materialize_sample_zips(sample_zips: list[Path], sample_dir: Path) -> list[Path]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    materialized = []
    for index, zip_path in enumerate(sample_zips, start=1):
        target = sample_dir / f"{index:03d}-{zip_path.name}"
        try:
            target.symlink_to(zip_path)
        except OSError:
            shutil.copy2(zip_path, target)
        materialized.append(target)
    return materialized


def sample_query(database_url: str) -> str:
    store = PostgresKnowledgeStore(database_url)
    for item in store.list_source_items():
        tokens = re.findall(r"[A-Za-z0-9_+-]{4,}|[\u4e00-\u9fff]{2,}", item.content_text)
        if tokens:
            return tokens[0]
    return "archive"


def _query_commands(args: argparse.Namespace, database_url: str, query: str) -> list[tuple[str, list[str]]]:
    commands = [
        (
            "search",
            [
                args.python,
                "-m",
                "pska_core.cli",
                "--database-url",
                database_url,
                "search",
                "--query",
                query,
                "--user-id",
                args.owner_user_id,
            ],
        )
    ]
    if not args.skip_llm:
        commands.append(
            (
                "agentic_search",
                [
                    args.python,
                    "-m",
                    "pska_core.cli",
                    "--database-url",
                    database_url,
                    "agentic-search",
                    "--query",
                    query,
                    "--user-id",
                    args.owner_user_id,
                ],
            )
        )
    return commands


def _checks_from_store(database_url: str, owner_user_id: str, *, skip_llm: bool) -> dict[str, bool]:
    store = PostgresKnowledgeStore(database_url)
    source_items = [item for item in store.list_source_items() if item.owner_user_id == owner_user_id]
    chunks = store.list_chunks_for_sources({item.source_item_id for item in source_items})
    jobs = store.list_jobs(job_type="digest_via_fastreact", limit=100)
    checks = {
        "sample_zips_found": True,
        "sources_imported": bool(source_items),
        "chunks_created": bool(chunks),
        "digest_job_scheduled": bool(jobs),
    }
    if not skip_llm:
        checks.update(
            {
                "entities_extracted": store.count_table("entities") > 0,
                "hyperedges_extracted": store.count_table("hyperedges") > 0,
            }
        )
    return checks


def _smoke_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    if not env.get("SSL_CERT_FILE"):
        try:
            import certifi  # type: ignore[import-not-found]

            env["SSL_CERT_FILE"] = certifi.where()
        except Exception:
            pass
    api_key_file = Path.home() / "api_key.txt"
    if api_key_file.exists():
        env.setdefault("PSKA_LLM_API_KEY_FILE", str(api_key_file))
    return env


def _step_result(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stdout": _scrub(result.stdout),
        "stderr": _scrub(result.stderr),
    }


def _scrub(text: str) -> str:
    for key_name in ("PSKA_LLM_API_KEY", "OPENAI_API_KEY", "FASTRACT_API_KEY"):
        secret = os.environ.get(key_name)
        if secret:
            text = text.replace(secret, "***")
    if len(text) > 4000:
        return "[truncated]\n" + text[-4000:]
    return text


if __name__ == "__main__":
    raise SystemExit(main())
