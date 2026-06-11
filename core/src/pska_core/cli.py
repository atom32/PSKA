from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Sequence

from pska_core.acl import ACLService
from pska_core.api import serve
from pska_core.embeddings import EmbeddingConfig, EmbeddingService, build_embedding_provider
from pska_core.enums import Visibility
from pska_core.extraction import ExtractionService
from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.ingest import IngestService
from pska_core.jobs import JOB_TYPES, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import ChannelIngestPayload
from pska_core.agentic import AgenticSearchService
from pska_core.retrieval import RetrievalService
from pska_core.serde import dumps
from pska_core.store_postgres import PostgresKnowledgeStore


DEFAULT_DATABASE_URL = "postgresql:///pska"
SMOKE_DATABASE_URL = "postgresql:///pska_smoke"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pska-core", description="PSKA Core local utilities")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("PSKA_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="PostgreSQL connection URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("db-check", help="Check PostgreSQL and pgvector availability")
    subparsers.add_parser("db-init", help="Apply the v1 schema migration")

    db_create = subparsers.add_parser("db-create", help="Create a local PostgreSQL database")
    db_create.add_argument("--name", default="pska_smoke")

    db_reset = subparsers.add_parser("db-reset", help="Drop and recreate a local PostgreSQL database, then apply schema")
    db_reset.add_argument("--name", default="pska_smoke")

    import_parser = subparsers.add_parser("import-twitter-zips", help="Import Twitter/X archive zip files")
    import_parser.add_argument("--input", type=Path, default=Path.home() / "Downloads" / "twitter_archive")
    import_parser.add_argument("--archive-root", type=Path, default=Path("archive/imports"))
    import_parser.add_argument("--owner-user-id", default="user_primary")
    import_parser.add_argument("--space-id", default="private_primary")
    import_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=Visibility.PRIVATE.value)
    import_parser.add_argument("--visible-team-ids", default="")
    _add_embedding_args(import_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    search_parser = subparsers.add_parser("search", help="Search PSKA Core")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--user-id", default="user_primary")
    search_parser.add_argument("--represented-user-id", default=None)
    search_parser.add_argument("--top-k", type=int, default=5)
    _add_embedding_args(search_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    agentic_parser = subparsers.add_parser("agentic-search", help="Run agentic PSKA search")
    agentic_parser.add_argument("--query", required=True)
    agentic_parser.add_argument("--user-id", default="user_primary")
    agentic_parser.add_argument("--represented-user-id", default=None)
    _add_embedding_args(agentic_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    embed_parser = subparsers.add_parser("embed-backfill", help="Backfill missing chunk embeddings")
    _add_embedding_args(embed_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "bge-m3"))
    embed_parser.add_argument("--batch-size", type=int, default=int(os.environ.get("PSKA_EMBEDDING_BATCH_SIZE", "16")))
    embed_parser.add_argument("--limit", type=int, default=None)

    ingest_parser = subparsers.add_parser("ingest-payload", help="Ingest a channel payload JSON file")
    ingest_parser.add_argument("payload", type=Path)

    extract_parser = subparsers.add_parser("extract-all", help="Extract entities/hyperedges from source items")
    extract_parser.add_argument("--owner-user-id", default=None)

    serve_parser = subparsers.add_parser("serve", help="Start local PSKA Core HTTP API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    subparsers.add_parser("mcp-server", help="Start PSKA stdio MCP server")

    smoke_parser = subparsers.add_parser("smoke-twitter-import", help="Reset pska_smoke, import zips, and run a search smoke")
    smoke_parser.add_argument("--input", type=Path, default=Path.home() / "Downloads" / "twitter_archive")
    smoke_parser.add_argument("--archive-root", type=Path, default=Path("archive/imports"))
    smoke_parser.add_argument("--query", default="")
    _add_embedding_args(smoke_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    submit_parser = subparsers.add_parser("job-submit", help="Queue a durable local job")
    submit_parser.add_argument("job_type", choices=sorted(JOB_TYPES))
    submit_parser.add_argument("--payload", type=Path, help="JSON file with job payload")
    submit_parser.add_argument("--max-attempts", type=int, default=3)
    submit_parser.add_argument("--run-now", action="store_true", help="Run queued jobs in this process after submit")

    run_parser = subparsers.add_parser("job-run", help="Run queued durable jobs in this process")
    run_parser.add_argument("--limit", type=int, default=1)
    run_parser.add_argument("--until-empty", action="store_true")

    worker_parser = subparsers.add_parser("job-worker", help="Continuously poll and run durable jobs")
    worker_parser.add_argument("--poll-interval", type=float, default=5.0)
    worker_parser.add_argument("--max-jobs", type=int, default=0, help="Stop after this many jobs; 0 means no limit")
    worker_parser.add_argument("--idle-limit", type=int, default=0, help="Stop after this many idle polls; 0 means no limit")
    worker_parser.add_argument("--recover-stale-seconds", type=int, default=0)

    status_parser = subparsers.add_parser("job-status", help="Show durable jobs and events")
    status_parser.add_argument("--job-id")
    status_parser.add_argument("--status")
    status_parser.add_argument("--limit", type=int, default=50)

    retry_parser = subparsers.add_parser("job-retry", help="Queue a failed or canceled job for retry")
    retry_parser.add_argument("job_id")

    recover_parser = subparsers.add_parser("job-recover", help="Recover stale running jobs")
    recover_parser.add_argument("--max-age-seconds", type=int, default=3600)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "db-check":
        return db_check(args.database_url)
    if args.command == "db-init":
        return db_init(args.database_url)
    if args.command == "db-create":
        return db_create(args.name)
    if args.command == "db-reset":
        return db_reset(args.name)
    if args.command == "import-twitter-zips":
        return import_twitter_zips(args)
    if args.command == "search":
        return search(args)
    if args.command == "agentic-search":
        return agentic_search(args)
    if args.command == "embed-backfill":
        return embed_backfill(args)
    if args.command == "ingest-payload":
        return ingest_payload(args)
    if args.command == "extract-all":
        return extract_all(args)
    if args.command == "serve":
        serve(args.host, args.port, args.database_url)
        return 0
    if args.command == "mcp-server":
        return MCPServer(args.database_url).run()
    if args.command == "smoke-twitter-import":
        return smoke_twitter_import(args)
    if args.command == "job-submit":
        return job_submit(args)
    if args.command == "job-run":
        return job_run(args)
    if args.command == "job-worker":
        return job_worker(args)
    if args.command == "job-status":
        return job_status(args)
    if args.command == "job-retry":
        return job_retry(args)
    if args.command == "job-recover":
        return job_recover(args)
    return 2


def db_check(database_url: str) -> int:
    psql = _psql_path()
    commands = [
        ("select current_database();", "database"),
        ("select default_version from pg_available_extensions where name = 'vector';", "pgvector"),
    ]
    for sql, label in commands:
        result = subprocess.run(
            [psql, database_url, "-Atc", sql],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            print(f"{label}: ERROR {result.stderr.strip()}", file=sys.stderr)
            return result.returncode
        value = result.stdout.strip() or "not available"
        print(f"{label}: {value}")
    return 0


def db_init(database_url: str) -> int:
    psql = _psql_path()
    migration_dir = Path(__file__).parent / "migrations"
    migrations = sorted(migration_dir.glob("*.sql"))
    if _table_exists(database_url, "users"):
        migrations = [migration for migration in migrations if not migration.name.startswith("001_")]
    for migration in migrations:
        result = subprocess.run([psql, database_url, "-f", str(migration)], check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


def db_create(name: str) -> int:
    if not _safe_database_name(name):
        print(f"Invalid database name: {name}", file=sys.stderr)
        return 2
    psql = _psql_path()
    exists = subprocess.run(
        [psql, "postgres", "-Atc", "select 1 from pg_database where datname = %s" % _sql_literal(name)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if exists.returncode != 0:
        print(exists.stderr.strip(), file=sys.stderr)
        return exists.returncode
    if exists.stdout.strip() == "1":
        print(f"database: {name} exists")
        return 0
    result = subprocess.run([_createdb_path(), name], check=False)
    if result.returncode == 0:
        print(f"database: {name} created")
    return result.returncode


def db_reset(name: str) -> int:
    if not _safe_database_name(name):
        print(f"Invalid database name: {name}", file=sys.stderr)
        return 2
    psql = _psql_path()
    commands = [
        f"drop database if exists {name} with (force);",
        f"create database {name};",
    ]
    for command in commands:
        result = subprocess.run([psql, "postgres", "-c", command], check=False)
        if result.returncode != 0:
            return result.returncode
    return db_init(f"postgresql:///{name}")


def import_twitter_zips(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    embedding_provider = _embedding_provider_from_args(args)
    importer = TwitterZipImporter(
        store,
        archive_root=args.archive_root,
        owner_user_id=args.owner_user_id,
        space_id=args.space_id,
        visibility=Visibility(args.visibility),
        visible_team_ids=[item.strip() for item in args.visible_team_ids.split(",") if item.strip()],
        embedding_provider=embedding_provider,
    )
    result = importer.import_directory(args.input)
    print(dumps(asdict(result)))
    return 1 if result.failed else 0


def search(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    user = store.get_user(args.user_id)
    response = RetrievalService(store, ACLService(store), embedding_provider=_embedding_provider_from_args(args)).search(
        args.query,
        user,
        represented_user_id=args.represented_user_id,
        top_k=args.top_k,
    )
    print(dumps(response))
    return 0


def agentic_search(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    user = store.get_user(args.user_id)
    response = AgenticSearchService(
        RetrievalService(store, ACLService(store), embedding_provider=_embedding_provider_from_args(args))
    ).search(
        args.query,
        user,
        represented_user_id=args.represented_user_id,
    )
    print(dumps(response))
    return 0


def embed_backfill(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    provider = _embedding_provider_from_args(args)
    if provider is None:
        print("Embedding provider is disabled; use --embedding-provider bge-m3", file=sys.stderr)
        return 2
    report = EmbeddingService(store, provider, batch_size=args.batch_size).backfill_missing(limit=args.limit)
    print(dumps(report))
    return 1 if report.failed else 0


def ingest_payload(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    data = __import__("json").loads(args.payload.read_text(encoding="utf-8"))
    item = IngestService(store).ingest_channel_payload(ChannelIngestPayload.from_mapping(data))
    print(dumps(item))
    return 0


def extract_all(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    reports = ExtractionService(store).extract_all_visible(owner_user_id=args.owner_user_id)
    print(dumps({"reports": reports}))
    return 0


def job_submit(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    payload = json.loads(args.payload.read_text(encoding="utf-8")) if args.payload else {}
    service = JobService(store)
    job = service.submit(args.job_type, payload, max_attempts=args.max_attempts)
    result: dict[str, object] = {"job": job}
    if args.run_now:
        result["run"] = service.run_available(limit=1)
    print(dumps(result))
    return 0


def job_run(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    service = JobService(store)
    report = service.run_until_empty(limit=args.limit if args.limit > 0 else None) if args.until_empty else service.run_available(limit=args.limit)
    print(dumps(report))
    return 1 if report.failed else 0


def job_worker(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    service = JobService(store)
    if args.recover_stale_seconds:
        recovered = service.recover_stale(max_age_seconds=args.recover_stale_seconds)
        if recovered:
            print(dumps({"recovered": recovered}), flush=True)
    processed = 0
    idle_polls = 0
    failed = 0
    while True:
        job = service.run_next()
        if job is None:
            idle_polls += 1
            if args.idle_limit and idle_polls >= args.idle_limit:
                break
            time.sleep(args.poll_interval)
            continue
        idle_polls = 0
        processed += 1
        if job.status == "failed":
            failed += 1
        print(dumps({"job": job}), flush=True)
        if args.max_jobs and processed >= args.max_jobs:
            break
    print(dumps({"processed": processed, "failed": failed}))
    return 1 if failed else 0


def job_status(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    if args.job_id:
        payload = {
            "job": store.get_job(args.job_id),
            "events": store.list_job_events(args.job_id),
        }
    else:
        payload = {"jobs": store.list_jobs(status=args.status, limit=args.limit)}
    print(dumps(payload))
    return 0


def job_retry(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    job = store.retry_job(args.job_id)
    print(dumps({"job": job}))
    return 0


def job_recover(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    jobs = JobService(store).recover_stale(max_age_seconds=args.max_age_seconds)
    print(dumps({"recovered": jobs}))
    return 0


def smoke_twitter_import(args: argparse.Namespace) -> int:
    reset_code = db_reset("pska_smoke")
    if reset_code != 0:
        return reset_code
    args.database_url = SMOKE_DATABASE_URL
    import_code = import_twitter_zips(args)
    if import_code != 0:
        return import_code
    query = args.query or _sample_query(PostgresKnowledgeStore(SMOKE_DATABASE_URL))
    search_args = argparse.Namespace(
        database_url=SMOKE_DATABASE_URL,
        query=query,
        user_id="user_primary",
        represented_user_id=None,
        top_k=5,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        embedding_dimensions=args.embedding_dimensions,
    )
    return search(search_args)


def _psql_path() -> str:
    for candidate in [
        os.environ.get("PSKA_PSQL"),
        shutil.which("psql"),
        "/usr/local/opt/postgresql@17/bin/psql",
        "/opt/homebrew/opt/postgresql@17/bin/psql",
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("psql not found. Add PostgreSQL to PATH or set PSKA_PSQL.")


def _createdb_path() -> str:
    psql = Path(_psql_path())
    candidate = psql.with_name("createdb")
    return str(candidate) if candidate.exists() else "createdb"


def _safe_database_name(name: str) -> bool:
    return bool(name) and all(char.isalnum() or char == "_" for char in name)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _table_exists(database_url: str, table: str) -> bool:
    psql = _psql_path()
    result = subprocess.run(
        [
            psql,
            database_url,
            "-Atc",
            "select to_regclass(%s) is not null;" % _sql_literal(table),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() in {"t", "true"}


def _add_embedding_args(parser: argparse.ArgumentParser, *, default_provider: str) -> None:
    parser.add_argument("--embedding-provider", default=default_provider)
    parser.add_argument("--embedding-model", default=os.environ.get("PSKA_EMBEDDING_MODEL", "BAAI/bge-m3"))
    parser.add_argument("--embedding-dimensions", type=int, default=int(os.environ.get("PSKA_EMBEDDING_DIMENSIONS", "1024")))


def _embedding_provider_from_args(args: argparse.Namespace):
    config = EmbeddingConfig(
        provider=getattr(args, "embedding_provider", os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled")),
        model=getattr(args, "embedding_model", os.environ.get("PSKA_EMBEDDING_MODEL", "BAAI/bge-m3")),
        dimensions=getattr(args, "embedding_dimensions", int(os.environ.get("PSKA_EMBEDDING_DIMENSIONS", "1024"))),
        batch_size=getattr(args, "batch_size", int(os.environ.get("PSKA_EMBEDDING_BATCH_SIZE", "16"))),
    )
    return build_embedding_provider(config)


def _sample_query(store: PostgresKnowledgeStore) -> str:
    for item in store.list_source_items():
        words = [part for part in item.content_text.split() if len(part) >= 2]
        if words:
            return words[0]
    return "twitter"


if __name__ == "__main__":
    raise SystemExit(main())
