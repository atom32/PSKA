from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from shlex import quote as shlex_quote
import shutil
import subprocess
import sys
import time
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pska_core.acl import ACLService
from pska_core.api import PSKAApi, serve
from pska_core.config import DEFAULT_DATABASE_URL, PSKAConfig
from pska_core.connectors import connector_state_from_mapping, connector_record_to_payload
from pska_core.embeddings import EmbeddingConfig, EmbeddingService, build_embedding_provider
from pska_core.enums import Visibility
from pska_core.extraction import ExtractionService
from pska_core.files_connector import scan_files
from pska_core.files_watcher import watch_files
from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.ingest import IngestService
from pska_core.jobs import JOB_TYPES, JobService
from pska_core.local_daemon import build_process_specs, run_supervisor
from pska_core.memory import MemoryService
from pska_core.mcp_server import MCPServer
from pska_core.models import ChannelIngestPayload, ReviewItem, SourceRef
from pska_core.agentic import AgenticSearchService
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.serde import dumps
from pska_core.store_postgres import PostgresKnowledgeStore


SMOKE_DATABASE_URL = "postgresql:///pska_smoke"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pska-core", description="PSKA Core local utilities")
    parser.add_argument("--config", type=Path, default=None, help="Path to PSKA JSON config")
    parser.add_argument(
        "--database-url",
        default=None,
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

    connector_ingest_parser = subparsers.add_parser("connector-ingest-record", help="Ingest a pska.connector_record.v1 JSON file")
    connector_ingest_parser.add_argument("record", type=Path)

    connector_state_parser = subparsers.add_parser("connector-state", help="Manage durable connector state")
    connector_state_parser.add_argument("action", choices=["list", "show", "upsert"], nargs="?", default="list")
    connector_state_parser.add_argument("connector_state_id", nargs="?")
    connector_state_parser.add_argument("--state", type=Path, help="pska.connector_state.v1 JSON file for upsert")
    connector_state_parser.add_argument("--connector-id")
    connector_state_parser.add_argument("--owner-user-id", default=None)
    connector_state_parser.add_argument("--enabled", choices=["true", "false"])
    connector_state_parser.add_argument("--scan-cursor")
    connector_state_parser.add_argument("--sync-status")
    connector_state_parser.add_argument("--permission-scope-json", default="")
    connector_state_parser.add_argument("--config-json", default="")

    files_scan_parser = subparsers.add_parser("files-scan", help="Scan an authorized local directory through the Files connector")
    files_scan_parser.add_argument("--root", type=Path, required=True)
    files_scan_parser.add_argument("--owner-user-id", default="user_primary")
    files_scan_parser.add_argument("--space-id", default="private_primary")
    files_scan_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=Visibility.PRIVATE.value)
    files_scan_parser.add_argument("--visible-team-ids", default="")
    files_scan_parser.add_argument("--ignore", action="append", default=[])
    files_scan_parser.add_argument("--max-bytes", type=int, default=1_000_000)
    _add_embedding_args(files_scan_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    files_sync_parser = subparsers.add_parser("files-sync", help="Scan configured Files connector roots from PSKA config")
    files_sync_parser.add_argument("--root", type=Path, action="append", default=[], help="Additional or override root to scan")
    files_sync_parser.add_argument("--owner-user-id", default=None)
    files_sync_parser.add_argument("--space-id", default=None)
    files_sync_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=None)
    files_sync_parser.add_argument("--ignore", action="append", default=[])
    files_sync_parser.add_argument("--max-bytes", type=int, default=None)
    _add_embedding_args(files_sync_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    files_watch_parser = subparsers.add_parser("files-watch", help="Watch configured Files connector roots and sync changes")
    files_watch_parser.add_argument("--root", type=Path, action="append", default=[], help="Additional or override root to watch")
    files_watch_parser.add_argument("--owner-user-id", default=None)
    files_watch_parser.add_argument("--space-id", default=None)
    files_watch_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=None)
    files_watch_parser.add_argument("--ignore", action="append", default=[])
    files_watch_parser.add_argument("--max-bytes", type=int, default=None)
    files_watch_parser.add_argument("--debounce-seconds", type=float, default=2.0)
    files_watch_parser.add_argument("--initial-sync", action="store_true")
    files_watch_parser.add_argument("--max-events", type=int, default=0, help="Stop after this many file events; 0 means no limit")
    _add_embedding_args(files_watch_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    extract_parser = subparsers.add_parser("extract-all", help="Extract entities/hyperedges from source items")
    extract_parser.add_argument("--owner-user-id", default=None)

    serve_parser = subparsers.add_parser("serve", help="Start local PSKA Core HTTP API")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    local_daemon_parser = subparsers.add_parser("local-daemon", help="Run PSKA service, worker, and digest scheduler under a local foreground supervisor")
    local_daemon_parser.add_argument("--no-worker", action="store_true")
    local_daemon_parser.add_argument("--no-digest-scheduler", action="store_true")
    local_daemon_parser.add_argument("--restart", action="store_true", help="Restart child processes if they exit")
    local_daemon_parser.add_argument("--worker-id", default="pska-worker-local")
    local_daemon_parser.add_argument("--poll-interval", type=float, default=5.0)
    local_daemon_parser.add_argument("--lease-seconds", type=int, default=300)
    local_daemon_parser.add_argument("--recover-stale-seconds", type=int, default=900)
    local_daemon_parser.add_argument("--digest-interval-seconds", type=float, default=300.0)
    local_daemon_parser.add_argument("--digest-limit", type=int, default=20)
    local_daemon_parser.add_argument("--digest-batch-size", type=int, default=20)
    local_daemon_parser.add_argument("--digest-max-backlog-jobs", type=int, default=10)

    mvp_bootstrap_parser = subparsers.add_parser("mvp-bootstrap", help="Initialize the MVP scope: DB, Twitter archive, local text roots, and digest backlog")
    mvp_bootstrap_parser.add_argument("--twitter-archive", type=Path, default=Path.home() / "Downloads" / "twitter_archive")
    mvp_bootstrap_parser.add_argument("--notes-root", type=Path, action="append", default=[])
    mvp_bootstrap_parser.add_argument("--archive-root", type=Path, default=Path("archive/imports"))
    mvp_bootstrap_parser.add_argument("--owner-user-id", default="user_primary")
    mvp_bootstrap_parser.add_argument("--space-id", default="private_primary")
    mvp_bootstrap_parser.add_argument("--skip-twitter", action="store_true")
    mvp_bootstrap_parser.add_argument("--skip-files", action="store_true")
    mvp_bootstrap_parser.add_argument("--skip-digest", action="store_true")
    mvp_bootstrap_parser.add_argument("--extract", action="store_true", help="Run initial LLM extraction after ingesting MVP sources")
    mvp_bootstrap_parser.add_argument("--digest-limit", type=int, default=20)
    mvp_bootstrap_parser.add_argument("--digest-batch-size", type=int, default=20)
    mvp_bootstrap_parser.add_argument("--dry-run", action="store_true")
    _add_embedding_args(mvp_bootstrap_parser, default_provider=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))

    mvp_status_parser = subparsers.add_parser("mvp-status", help="Show MVP readiness, metrics, and next actions")
    mvp_status_parser.add_argument("--summary", action="store_true", help="Print a compact human-scale MVP status summary")

    daily_status_parser = subparsers.add_parser("daily-status", help="Show deterministic daily PSKA readiness, backlog, and next commands")
    daily_status_parser.add_argument("--owner-user-id", default="user_primary")
    daily_status_parser.add_argument("--limit", type=int, default=5, help="Maximum pending reviews and failed jobs to include")

    digest_worker_command_parser = subparsers.add_parser(
        "fastreact-digest-worker-command",
        help="Print the Fastreact-side PSKA digest worker command for this PSKA config",
    )
    digest_worker_command_parser.add_argument("--fastreact-root", type=Path, default=Path.home() / "Fastreact" / "fastreact-nano")
    digest_worker_command_parser.add_argument("--python", default="python3")
    digest_worker_command_parser.add_argument("--pska-url", default=None)
    digest_worker_command_parser.add_argument("--fastreact-url", default=None)
    digest_worker_command_parser.add_argument("--batch-limit", type=int, default=20)
    digest_worker_command_parser.add_argument("--represented-user-id", default="user_primary")

    service_check_parser = subparsers.add_parser("service-check", help="Check a running PSKA online service contract")
    service_check_parser.add_argument("--url", default=None)
    service_check_parser.add_argument("--service-token", default=None)
    service_check_parser.add_argument("--timeout-seconds", type=float, default=5.0)
    service_check_parser.add_argument("--expected-database-url", default=None)

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
    run_parser.add_argument("--worker-id", default=None)
    run_parser.add_argument("--lease-seconds", type=int, default=300)

    worker_parser = subparsers.add_parser("job-worker", help="Continuously poll and run durable jobs")
    worker_parser.add_argument("--poll-interval", type=float, default=5.0)
    worker_parser.add_argument("--max-jobs", type=int, default=0, help="Stop after this many jobs; 0 means no limit")
    worker_parser.add_argument("--idle-limit", type=int, default=0, help="Stop after this many idle polls; 0 means no limit")
    worker_parser.add_argument("--recover-stale-seconds", type=int, default=0)
    worker_parser.add_argument("--worker-id", default=None)
    worker_parser.add_argument("--lease-seconds", type=int, default=300)

    status_parser = subparsers.add_parser("job-status", help="Show durable jobs and events")
    status_parser.add_argument("--job-id")
    status_parser.add_argument("--status")
    status_parser.add_argument("--job-type")
    status_parser.add_argument("--limit", type=int, default=50)

    jobs_parser = subparsers.add_parser("jobs", help="List durable jobs or show job stats")
    jobs_parser.add_argument("action", choices=["list", "stats", "show"], nargs="?", default="list")
    jobs_parser.add_argument("job_id", nargs="?")
    jobs_parser.add_argument("--status")
    jobs_parser.add_argument("--job-type")
    jobs_parser.add_argument("--limit", type=int, default=50)

    retry_parser = subparsers.add_parser("job-retry", help="Queue a failed or canceled job for retry")
    retry_parser.add_argument("job_id")

    cancel_parser = subparsers.add_parser("job-cancel", help="Cancel a queued or running job")
    cancel_parser.add_argument("job_id")
    cancel_parser.add_argument("--reason", default="")

    recover_parser = subparsers.add_parser("job-recover", help="Recover stale running jobs")
    recover_parser.add_argument("--max-age-seconds", type=int, default=3600)

    digest_schedule_parser = subparsers.add_parser("digest-schedule", help="Schedule digest_via_fastreact jobs from source backlog")
    digest_schedule_parser.add_argument("--owner-user-id", default="user_primary")
    digest_schedule_parser.add_argument("--source-item-id", action="append", dest="source_item_ids", default=[])
    digest_schedule_parser.add_argument("--limit", type=int, default=20)
    digest_schedule_parser.add_argument("--batch-size", type=int, default=20)
    digest_schedule_parser.add_argument("--priority", type=int, default=0)
    digest_schedule_parser.add_argument("--max-attempts", type=int, default=3)
    digest_schedule_parser.add_argument("--retry-backoff-seconds", type=int, default=60)
    digest_schedule_parser.add_argument("--quota-window-seconds", type=int, default=0, help="Optional scheduling quota window; 0 disables quota")
    digest_schedule_parser.add_argument("--max-jobs-per-window", type=int, default=0, help="Optional max digest jobs per quota window; 0 disables quota")
    digest_schedule_parser.add_argument("--force", action="store_true")
    digest_schedule_parser.add_argument("--reason", default="")

    digest_scheduler_parser = subparsers.add_parser("digest-scheduler", help="Foreground periodic digest backlog scheduler")
    digest_scheduler_parser.add_argument("--owner-user-id", default="user_primary")
    digest_scheduler_parser.add_argument("--interval-seconds", type=float, default=60.0)
    digest_scheduler_parser.add_argument("--max-cycles", type=int, default=0, help="Stop after this many scheduler cycles; 0 means no limit")
    digest_scheduler_parser.add_argument("--idle-limit", type=int, default=0, help="Stop after this many idle cycles; 0 means no limit")
    digest_scheduler_parser.add_argument("--limit", type=int, default=20)
    digest_scheduler_parser.add_argument("--batch-size", type=int, default=20)
    digest_scheduler_parser.add_argument("--priority", type=int, default=0)
    digest_scheduler_parser.add_argument("--max-attempts", type=int, default=3)
    digest_scheduler_parser.add_argument("--retry-backoff-seconds", type=int, default=60)
    digest_scheduler_parser.add_argument("--quota-window-seconds", type=int, default=0, help="Optional scheduling quota window; 0 disables quota")
    digest_scheduler_parser.add_argument("--max-jobs-per-window", type=int, default=0, help="Optional max digest jobs per quota window; 0 disables quota")
    digest_scheduler_parser.add_argument("--max-backlog-jobs", type=int, default=10)
    digest_scheduler_parser.add_argument("--recover-stale-seconds", type=int, default=0)
    digest_scheduler_parser.add_argument("--force", action="store_true")
    digest_scheduler_parser.add_argument("--reason", default="periodic digest scheduler")

    review_list_parser = subparsers.add_parser("review-list", help="List review items awaiting or recording human decisions")
    review_list_parser.add_argument("--status", default=None)
    review_list_parser.add_argument("--owner-user-id", default=None)
    review_list_parser.add_argument("--limit", type=int, default=50)
    review_list_parser.add_argument("--summary", action="store_true", help="Print compact review item rows")

    review_approve_parser = subparsers.add_parser("review-approve", help="Approve a pending review item")
    review_approve_parser.add_argument("review_item_id")
    review_approve_parser.add_argument("--actor-user-id", default="user_primary")
    review_approve_parser.add_argument("--reason", default="")
    review_approve_parser.add_argument("--apply", action="store_true")

    review_reject_parser = subparsers.add_parser("review-reject", help="Reject a pending review item")
    review_reject_parser.add_argument("review_item_id")
    review_reject_parser.add_argument("--actor-user-id", default="user_primary")
    review_reject_parser.add_argument("--reason", default="")

    review_apply_parser = subparsers.add_parser("review-apply", help="Apply an approved review item")
    review_apply_parser.add_argument("review_item_id")
    review_apply_parser.add_argument("--actor-user-id", default="user_primary")
    review_apply_parser.add_argument("--reason", default="")

    profile_parser = subparsers.add_parser("profile-propose", help="Propose a profile card update")
    profile_parser.add_argument("--owner-user-id", default="user_primary")
    profile_parser.add_argument("--profile-delta-json", required=True)
    profile_parser.add_argument("--source-ref-json", action="append", default=[])
    profile_parser.add_argument("--sensitivity", default="normal")
    profile_parser.add_argument("--confidence", type=float, default=0.8)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = PSKAConfig.load(args.config)
    config.apply_to_env()
    args.database_url = args.database_url or config.database.url
    if args.command == "service-check":
        args.url = args.url or os.environ.get("PSKA_SERVICE_URL") or f"http://{config.service.host}:{config.service.port}"
        args.service_token = args.service_token or os.environ.get("PSKA_SERVICE_TOKEN")
        args.expected_database_url = args.expected_database_url or config.database.url
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
    if args.command == "connector-ingest-record":
        return connector_ingest_record(args)
    if args.command == "connector-state":
        return connector_state(args)
    if args.command == "files-scan":
        return files_scan(args)
    if args.command == "files-sync":
        return files_sync(args, config)
    if args.command == "files-watch":
        return files_watch(args, config)
    if args.command == "extract-all":
        return extract_all(args)
    if args.command == "serve":
        serve(args.host or config.service.host, args.port or config.service.port, args.database_url)
        return 0
    if args.command == "local-daemon":
        return local_daemon(args, config)
    if args.command == "mvp-bootstrap":
        return mvp_bootstrap(args)
    if args.command == "mvp-status":
        return mvp_status(args)
    if args.command == "daily-status":
        return daily_status(args)
    if args.command == "fastreact-digest-worker-command":
        return fastreact_digest_worker_command(args, config)
    if args.command == "service-check":
        return service_check(args)
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
    if args.command == "jobs":
        return jobs(args)
    if args.command == "job-retry":
        return job_retry(args)
    if args.command == "job-cancel":
        return job_cancel(args)
    if args.command == "job-recover":
        return job_recover(args)
    if args.command == "digest-schedule":
        return digest_schedule(args)
    if args.command == "digest-scheduler":
        return digest_scheduler(args)
    if args.command == "review-list":
        return review_list(args)
    if args.command == "review-approve":
        return review_approve(args)
    if args.command == "review-reject":
        return review_reject(args)
    if args.command == "review-apply":
        return review_apply(args)
    if args.command == "profile-propose":
        return profile_propose(args)
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


def connector_ingest_record(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    data = json.loads(args.record.read_text(encoding="utf-8"))
    payload = connector_record_to_payload(data)
    item = IngestService(store).ingest_channel_payload(payload)
    print(dumps({"source_item": item, "channel_payload": payload}))
    return 0


def connector_state(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    if args.action == "show":
        if not args.connector_state_id:
            print("connector-state show requires connector_state_id", file=sys.stderr)
            return 2
        print(dumps({"connector_state": store.get_connector_state(args.connector_state_id)}))
        return 0
    if args.action == "upsert":
        payload = json.loads(args.state.read_text(encoding="utf-8")) if args.state else {}
        if args.connector_state_id:
            payload["connector_state_id"] = args.connector_state_id
        if args.connector_id:
            payload["connector_id"] = args.connector_id
        if args.owner_user_id:
            payload["owner_user_id"] = args.owner_user_id
        if args.enabled:
            payload["enabled"] = args.enabled == "true"
        if args.scan_cursor is not None:
            payload["scan_cursor"] = args.scan_cursor
        if args.sync_status:
            payload["sync_status"] = args.sync_status
        if args.permission_scope_json:
            payload["permission_scope"] = json.loads(args.permission_scope_json)
        if args.config_json:
            payload["config"] = json.loads(args.config_json)
        state = connector_state_from_mapping(payload)
        print(dumps({"connector_state": store.upsert_connector_state(state)}))
        return 0
    states = store.list_connector_states(owner_user_id=args.owner_user_id, connector_id=args.connector_id)
    print(dumps({"connector_states": states}))
    return 0


def files_scan(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    report = scan_files(
        store,
        root=args.root,
        owner_user_id=args.owner_user_id,
        space_id=args.space_id,
        visibility=Visibility(args.visibility),
        visible_team_ids=[item.strip() for item in args.visible_team_ids.split(",") if item.strip()],
        ignore=list(args.ignore or []),
        max_bytes=args.max_bytes,
        embedding_provider=_embedding_provider_from_args(args),
    )
    print(dumps(report))
    return 1 if report.failed else 0


def files_sync(args: argparse.Namespace, config: PSKAConfig) -> int:
    roots = list(args.root or []) or list(config.files.roots)
    if not roots:
        print(dumps({
            "ok": False,
            "error": "No files roots configured. Add files.roots to .pska/config.json or pass --root <path>.",
            "reports": [],
        }))
        return 1
    store = PostgresKnowledgeStore(args.database_url)
    reports = []
    failed = []
    for root in roots:
        try:
            report = scan_files(
                store,
                root=root,
                owner_user_id=args.owner_user_id or config.files.owner_user_id,
                space_id=args.space_id or config.files.space_id,
                visibility=Visibility(args.visibility or config.files.visibility),
                ignore=[*config.files.ignore, *(args.ignore or [])],
                max_bytes=args.max_bytes or config.files.max_bytes,
                embedding_provider=_embedding_provider_from_args(args),
            )
            reports.append(report)
            failed.extend(report.failed)
        except Exception as exc:  # noqa: BLE001 - report all roots together.
            failed.append({"root": str(root), "error": f"{type(exc).__name__}: {exc}"})
    payload = {
        "ok": not failed,
        "database_url": args.database_url,
        "roots": [str(root.expanduser()) for root in roots],
        "reports": reports,
        "totals": {
            "roots": len(roots),
            "scanned": sum(report.scanned for report in reports),
            "ingested": sum(report.ingested for report in reports),
            "new_files": sum(report.new_files for report in reports),
            "changed_files": sum(report.changed_files for report in reports),
            "unchanged_files": sum(report.unchanged_files for report in reports),
            "moved_files": sum(report.moved_files for report in reports),
            "missing_files": sum(report.missing_files for report in reports),
            "skipped": sum(len(report.skipped) for report in reports),
            "failed": len(failed),
        },
        "failed": failed,
    }
    print(dumps(payload))
    return 0 if payload["ok"] else 1


def files_watch(args: argparse.Namespace, config: PSKAConfig) -> int:
    roots = list(args.root or []) or list(config.files.roots)
    if not roots:
        print(dumps({
            "ok": False,
            "error": "No files roots configured. Add files.roots to .pska/config.json or pass --root <path>.",
        }))
        return 1
    store = PostgresKnowledgeStore(args.database_url)
    print(
        dumps(
            {
                "event": "files_watch_started",
                "database_url": args.database_url,
                "roots": [str(root.expanduser()) for root in roots],
                "debounce_seconds": args.debounce_seconds,
                "initial_sync": args.initial_sync,
            }
        ),
        flush=True,
    )

    def on_report(report) -> None:  # noqa: ANN001 - report is JSON-serializable through dumps.
        print(
            dumps(
                {
                    "event": "files_watch_synced",
                    "root": report.root,
                    "scanned": report.scanned,
                    "ingested": report.ingested,
                    "skipped": len(report.skipped),
                    "failed": len(report.failed),
                    "source_item_ids": report.source_item_ids,
                }
            ),
            flush=True,
        )

    try:
        summary = watch_files(
            store,
            roots=roots,
            owner_user_id=args.owner_user_id or config.files.owner_user_id,
            space_id=args.space_id or config.files.space_id,
            visibility=Visibility(args.visibility or config.files.visibility),
            ignore=[*config.files.ignore, *(args.ignore or [])],
            max_bytes=args.max_bytes or config.files.max_bytes,
            debounce_seconds=args.debounce_seconds,
            initial_sync=args.initial_sync,
            max_events=args.max_events,
            on_report=on_report,
            embedding_provider=_embedding_provider_from_args(args),
        )
    except Exception as exc:  # noqa: BLE001 - CLI should report optional dependency/setup errors cleanly.
        print(dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(dumps({"event": "files_watch_stopped", "summary": summary}), flush=True)
    return 0


def extract_all(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    reports = ExtractionService(store).extract_all_visible(owner_user_id=args.owner_user_id)
    print(dumps({"reports": reports}))
    return 0


def service_check(args: argparse.Namespace) -> int:
    base_url = str(args.url).rstrip("/")
    headers = _service_check_headers(args.service_token)
    checks: dict[str, Any] = {
        "health": _service_check_request(base_url, "GET", "/health", headers=headers, timeout_seconds=args.timeout_seconds),
        "ready": _service_check_request(base_url, "GET", "/ready", headers=headers, timeout_seconds=args.timeout_seconds),
        "mcp_tools": _service_check_request(
            base_url,
            "POST",
            "/mcp",
            headers=headers,
            timeout_seconds=args.timeout_seconds,
            payload={"jsonrpc": "2.0", "id": "service-check", "method": "tools/list", "params": {}},
        ),
    }
    mcp_payload = checks["mcp_tools"].get("payload") if checks["mcp_tools"].get("ok") else {}
    tools = (((mcp_payload or {}).get("result") or {}).get("tools") or []) if isinstance(mcp_payload, dict) else []
    tool_names = [tool.get("name") for tool in tools if isinstance(tool, dict) and tool.get("name")]
    checks["mcp_tools"]["tool_names"] = tool_names
    checks["mcp_tools"]["has_pska_search"] = "pska_search" in tool_names
    health_payload = checks["health"].get("payload") if checks["health"].get("ok") else {}
    actual_database_url = health_payload.get("database") if isinstance(health_payload, dict) else None
    expected_database_url = str(getattr(args, "expected_database_url", None) or "")
    checks["database_alignment"] = {
        "ok": not expected_database_url or actual_database_url == expected_database_url,
        "expected": expected_database_url or None,
        "actual": actual_database_url,
    }
    ok = (
        checks["health"].get("ok") is True
        and checks["ready"].get("ok") is True
        and checks["ready"].get("payload", {}).get("ok") is True
        and checks["mcp_tools"].get("ok") is True
        and checks["mcp_tools"]["has_pska_search"]
        and checks["database_alignment"]["ok"] is True
    )
    print(dumps({"ok": ok, "url": base_url, "checks": checks}))
    return 0 if ok else 1


def local_daemon(args: argparse.Namespace, config: PSKAConfig) -> int:
    specs = build_process_specs(
        config_path=args.config,
        config=config,
        database_url=args.database_url,
        include_worker=not args.no_worker,
        include_digest_scheduler=not args.no_digest_scheduler,
        worker_id=args.worker_id,
        poll_interval=args.poll_interval,
        lease_seconds=args.lease_seconds,
        recover_stale_seconds=args.recover_stale_seconds,
        digest_interval_seconds=args.digest_interval_seconds,
        digest_limit=args.digest_limit,
        digest_batch_size=args.digest_batch_size,
        digest_max_backlog_jobs=args.digest_max_backlog_jobs,
    )
    return run_supervisor(specs, restart=args.restart)


def mvp_bootstrap(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "database_url": args.database_url,
        "dry_run": bool(args.dry_run),
        "steps": [],
        "next_actions": [
            "Start PSKA with: ./scripts/pska local-daemon",
            "Start Fastreact and its PSKA digest worker when you want LLM digest candidates.",
            "Check status with: ./scripts/pska mvp-status",
        ],
    }
    if args.dry_run:
        report["steps"].append({"name": "db_init", "would_run": True})
    else:
        db_init_returncode = db_init(args.database_url)
        report["steps"].append({"name": "db_init", "returncode": db_init_returncode})
        if db_init_returncode != 0:
            report["ok"] = False
            report["error"] = "db_init_failed"
            print(dumps(report))
            return db_init_returncode

    store = PostgresKnowledgeStore(args.database_url)
    embedding_provider = _embedding_provider_from_args(args)

    if args.skip_twitter:
        report["steps"].append({"name": "twitter_archive", "skipped": True, "reason": "--skip-twitter"})
    elif not args.twitter_archive.expanduser().exists():
        report["steps"].append({"name": "twitter_archive", "skipped": True, "reason": "archive directory not found", "path": str(args.twitter_archive)})
    elif args.dry_run:
        zip_count = len(list(args.twitter_archive.expanduser().glob("*.zip")))
        report["steps"].append({"name": "twitter_archive", "would_import_zip_count": zip_count, "path": str(args.twitter_archive)})
    else:
        importer = TwitterZipImporter(
            store,
            archive_root=args.archive_root,
            owner_user_id=args.owner_user_id,
            space_id=args.space_id,
            visibility=Visibility.PRIVATE,
            embedding_provider=embedding_provider,
        )
        result = importer.import_directory(args.twitter_archive)
        report["steps"].append({"name": "twitter_archive", "result": asdict(result)})

    if args.skip_files:
        report["steps"].append({"name": "files", "skipped": True, "reason": "--skip-files"})
    else:
        for root in args.notes_root:
            if args.dry_run:
                report["steps"].append({"name": "files", "would_scan": str(root)})
                continue
            try:
                scan = scan_files(
                    store,
                    root=root,
                    owner_user_id=args.owner_user_id,
                    space_id=args.space_id,
                    visibility=Visibility.PRIVATE,
                    embedding_provider=embedding_provider,
                )
                report["steps"].append({"name": "files", "root": str(root), "result": scan})
            except Exception as exc:  # noqa: BLE001 - bootstrap should continue across optional roots.
                report["steps"].append({"name": "files", "root": str(root), "error": f"{type(exc).__name__}: {exc}"})

    if args.skip_digest:
        report["steps"].append({"name": "digest_schedule", "skipped": True, "reason": "--skip-digest"})
    elif args.dry_run:
        report["steps"].append({"name": "digest_schedule", "would_run": True, "limit": args.digest_limit, "batch_size": args.digest_batch_size})
    else:
        digest = PSKAApi(args.database_url).schedule_digest(
            {
                "owner_user_id": args.owner_user_id,
                "limit": args.digest_limit,
                "batch_size": args.digest_batch_size,
                "reason": "mvp bootstrap",
            }
        )
        report["steps"].append({"name": "digest_schedule", "result": digest})

    if args.extract:
        if args.dry_run:
            report["steps"].append({"name": "extract_all", "would_run": True, "owner_user_id": args.owner_user_id})
        else:
            reports = ExtractionService(store).extract_all_visible(owner_user_id=args.owner_user_id)
            report["steps"].append({"name": "extract_all", "reports": reports})

    try:
        report["status"] = _mvp_status_payload(args.database_url)
        report["ok"] = bool(report["status"].get("ok"))
    except Exception as exc:  # noqa: BLE001 - bootstrap should produce an actionable report.
        report["ok"] = False
        report["status_error"] = f"{type(exc).__name__}: {exc}"
    print(dumps(report))
    return 0


def mvp_status(args: argparse.Namespace) -> int:
    payload = _mvp_status_payload(args.database_url)
    print(dumps(_mvp_status_summary(payload) if args.summary else payload))
    return 0


def daily_status(args: argparse.Namespace) -> int:
    payload = _daily_status_payload(args.database_url, owner_user_id=args.owner_user_id, limit=args.limit)
    print(dumps(payload))
    return 0


def fastreact_digest_worker_command(args: argparse.Namespace, config: PSKAConfig) -> int:
    payload = _fastreact_digest_worker_command_payload(args, config)
    print(dumps(payload))
    return 0 if payload["ok"] else 1


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
    service = JobService(store, worker_id=args.worker_id or _default_worker_id(), lease_seconds=args.lease_seconds)
    report = service.run_until_empty(limit=args.limit if args.limit > 0 else None) if args.until_empty else service.run_available(limit=args.limit)
    print(dumps(report))
    return 1 if report.failed else 0


def job_worker(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    service = JobService(store, worker_id=args.worker_id or _default_worker_id(), lease_seconds=args.lease_seconds)
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
        payload = {"jobs": store.list_jobs(status=args.status, job_type=args.job_type, limit=args.limit)}
    print(dumps(payload))
    return 0


def jobs(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    if args.action == "show":
        if not args.job_id:
            print("jobs show requires job_id", file=sys.stderr)
            return 2
        payload = {
            "job": store.get_job(args.job_id),
            "events": store.list_job_events(args.job_id),
        }
    elif args.action == "stats":
        payload = {"stats": _job_stats(store, limit=args.limit)}
    else:
        payload = {"jobs": store.list_jobs(status=args.status, job_type=args.job_type, limit=args.limit)}
    print(dumps(payload))
    return 0


def job_retry(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    job = store.retry_job(args.job_id)
    print(dumps({"job": job}))
    return 0


def job_cancel(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    job = store.cancel_job(args.job_id, reason=args.reason)
    print(dumps({"job": job}))
    return 0


def job_recover(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    jobs = JobService(store).recover_stale(max_age_seconds=args.max_age_seconds)
    print(dumps({"recovered": jobs}))
    return 0


def digest_schedule(args: argparse.Namespace) -> int:
    payload = _digest_schedule_payload(args)
    print(dumps(PSKAApi(args.database_url).schedule_digest(payload)))
    return 0


def digest_scheduler(args: argparse.Namespace) -> int:
    api = PSKAApi(args.database_url)
    processed = 0
    idle_cycles = 0
    while True:
        if args.recover_stale_seconds:
            recovered = api.store.recover_stale_jobs(max_age_seconds=args.recover_stale_seconds)
            if recovered:
                print(dumps({"event": "stale_recovered", "recovered": recovered}), flush=True)

        stats = api.job_stats()["stats"]
        backlog_jobs = int((stats.get("digest_backlog") or {}).get("jobs") or 0)
        if args.max_backlog_jobs and backlog_jobs >= args.max_backlog_jobs:
            result = {
                "event": "digest_scheduler_cycle",
                "scheduled": False,
                "reason": "backlog_limit_reached",
                "digest_backlog_jobs": backlog_jobs,
            }
        else:
            scheduled = api.schedule_digest(_digest_schedule_payload(args))
            result = {
                "event": "digest_scheduler_cycle",
                "scheduled": bool(scheduled.get("scheduled_source_item_ids")),
                "digest": scheduled,
            }

        processed += 1
        if result["scheduled"]:
            idle_cycles = 0
        else:
            idle_cycles += 1
        result["cycle"] = processed
        result["idle_cycles"] = idle_cycles
        print(dumps(result), flush=True)

        if args.max_cycles and processed >= args.max_cycles:
            break
        if args.idle_limit and idle_cycles >= args.idle_limit:
            break
        time.sleep(max(0.0, args.interval_seconds))

    print(dumps({"processed": processed, "idle_cycles": idle_cycles}))
    return 0


def review_list(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    items = store.list_review_items()
    print(dumps(_review_items_payload(items, status=args.status, owner_user_id=args.owner_user_id, limit=args.limit, summary=args.summary)))
    return 0


def review_approve(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    service = ReviewService(store)
    if args.apply:
        review_item = service.approve_and_apply(
            args.review_item_id,
            actor_user_id=args.actor_user_id,
            reason=args.reason,
        )
    else:
        review_item = service.approve(
            args.review_item_id,
            actor_user_id=args.actor_user_id,
            reason=args.reason,
        )
    print(dumps({"review_item": review_item}))
    return 0


def _review_items_payload(
    items: Sequence[ReviewItem],
    *,
    status: str | None = None,
    owner_user_id: str | None = None,
    limit: int = 50,
    summary: bool = False,
) -> dict[str, Any]:
    filtered = [
        item
        for item in items
        if (status is None or item.status == status)
        and (owner_user_id is None or item.owner_user_id == owner_user_id)
    ]
    limited = filtered[: max(0, limit)]
    if summary:
        payload_items = [
            _review_item_summary(item)
            for item in limited
        ]
    else:
        payload_items = list(limited)
    return {
        "review_items": payload_items,
        "count": len(payload_items),
        "total_matching": len(filtered),
        "limit": max(0, limit),
    }


def _review_item_summary(item: ReviewItem) -> dict[str, Any]:
    review_type = item.review_type.value if hasattr(item.review_type, "value") else str(item.review_type)
    source_refs = _review_source_refs(item.proposal)
    apply_supported = review_type in {"profile_update", "share_proposal"}
    return {
        "review_item_id": item.review_item_id,
        "owner_user_id": item.owner_user_id,
        "review_type": review_type,
        "status": item.status,
        "title": item.title,
        "confidence": _review_confidence(item.proposal),
        "source_refs": source_refs,
        "source_ref_status": "present" if source_refs else "missing",
        "created_at": item.created_at,
        "recommended_actions": _review_recommended_actions(item, apply_supported=apply_supported),
        "apply_supported": apply_supported,
        "can_apply_now": item.status == "approved" and apply_supported,
    }


def _review_confidence(proposal: dict[str, Any]) -> float | None:
    value = proposal.get("confidence")
    if value is None and isinstance(proposal.get("candidate"), dict):
        value = proposal["candidate"].get("confidence")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _review_source_refs(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    refs = proposal.get("source_refs")
    if not isinstance(refs, list) and isinstance(proposal.get("candidate"), dict):
        refs = proposal["candidate"].get("source_refs")
    if not isinstance(refs, list):
        return []
    allowed = set(SourceRef.__dataclass_fields__)
    normalized = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        normalized_ref = {key: value for key, value in ref.items() if key in allowed and value}
        if normalized_ref:
            normalized.append(normalized_ref)
    return normalized


def _review_recommended_actions(item: ReviewItem, *, apply_supported: bool) -> list[str]:
    base = f"./scripts/pska review-approve {item.review_item_id}"
    reject = f"./scripts/pska review-reject {item.review_item_id}"
    if item.status == "pending":
        actions = [base, reject]
        if apply_supported:
            actions.insert(1, f"{base} --apply")
        return actions
    if item.status == "approved":
        actions = [reject]
        if apply_supported:
            actions.insert(0, f"./scripts/pska review-apply {item.review_item_id}")
        return actions
    return []


def review_reject(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    review_item = ReviewService(store).reject(
        args.review_item_id,
        actor_user_id=args.actor_user_id,
        reason=args.reason,
    )
    print(dumps({"review_item": review_item}))
    return 0


def review_apply(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    review_item = ReviewService(store).apply(
        args.review_item_id,
        actor_user_id=args.actor_user_id,
        reason=args.reason,
    )
    print(dumps({"review_item": review_item}))
    return 0


def profile_propose(args: argparse.Namespace) -> int:
    profile_delta = json.loads(args.profile_delta_json)
    if not isinstance(profile_delta, dict) or not profile_delta:
        raise ValueError("--profile-delta-json must be a non-empty JSON object")

    source_refs = []
    for raw_ref in args.source_ref_json:
        ref = json.loads(raw_ref)
        if not isinstance(ref, dict):
            raise ValueError("--source-ref-json must be a JSON object")
        allowed_keys = set(SourceRef.__dataclass_fields__)
        source_refs.append(SourceRef(**{key: value for key, value in ref.items() if key in allowed_keys}))

    store = PostgresKnowledgeStore(args.database_url)
    result = MemoryService(store).propose_profile_update(
        owner_user_id=args.owner_user_id,
        profile_delta=profile_delta,
        source_refs=source_refs,
        sensitivity=args.sensitivity,
        confidence=args.confidence,
    )
    if isinstance(result, ReviewItem):
        print(dumps({"review_item": result}))
    else:
        print(dumps({"profile_card": result}))
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
    parser.add_argument("--embedding-provider", default=None)
    parser.add_argument("--embedding-model", default=None)
    parser.add_argument("--embedding-dimensions", type=int, default=None)


def _embedding_provider_from_args(args: argparse.Namespace):
    config = EmbeddingConfig(
        provider=getattr(args, "embedding_provider", None) or os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"),
        model=getattr(args, "embedding_model", None) or os.environ.get("PSKA_EMBEDDING_MODEL", "BAAI/bge-m3"),
        dimensions=getattr(args, "embedding_dimensions", None) or int(os.environ.get("PSKA_EMBEDDING_DIMENSIONS", "1024")),
        batch_size=getattr(args, "batch_size", int(os.environ.get("PSKA_EMBEDDING_BATCH_SIZE", "16"))),
    )
    return build_embedding_provider(config)


def _sample_query(store: PostgresKnowledgeStore) -> str:
    for item in store.list_source_items():
        words = [part for part in item.content_text.split() if len(part) >= 2]
        if words:
            return words[0]
    return "twitter"


def _default_worker_id() -> str:
    return f"pska-worker-{os.getpid()}"


def _mvp_status_payload(database_url: str) -> dict[str, Any]:
    api = PSKAApi(database_url)
    ready = api.ready()
    try:
        metrics = api.metrics()
    except Exception as exc:  # noqa: BLE001 - MVP status should report schema drift instead of crashing.
        metrics = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "index": {}, "connectors": {}}
    try:
        jobs = api.job_stats()["stats"]
    except Exception as exc:  # noqa: BLE001
        jobs = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "digest_backlog": {}, "by_status": {}}
    index = metrics.get("index") or {}
    connectors = metrics.get("connectors") or {}
    try:
        review_items = api.store.list_review_items()
    except Exception:
        review_items = []
    pending_reviews = [item for item in review_items if item.status == "pending"]
    payload = {
        "ok": bool(ready.get("ok")) and int(index.get("source_items") or 0) > 0,
        "database_url": database_url,
        "ready": ready,
        "metrics": metrics,
        "jobs": jobs,
        "pending_review_items": len(pending_reviews),
        "mvp_scope": {
            "data_sources": ["twitter_archive", "local_text_files"],
            "deferred": ["mail", "photos", "nas", "browser_history", "deep_git_sync", "pdf_word_complex_parsing"],
        },
    }
    payload["next_actions"] = _mvp_next_actions(payload, connectors=connectors)
    return payload


def _mvp_status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    ready = payload.get("ready") or {}
    checks = ready.get("checks") or {}
    metrics = payload.get("metrics") or {}
    index = metrics.get("index") or {}
    connectors = metrics.get("connectors") or {}
    jobs = payload.get("jobs") or {}
    fastreact = checks.get("fastreact") or {}
    return {
        "ok": bool(payload.get("ok")),
        "database_url": payload.get("database_url"),
        "database_ok": bool((checks.get("database") or {}).get("ok")),
        "schema_ok": bool((checks.get("schema") or {}).get("ok")),
        "mcp_ok": bool((checks.get("mcp") or {}).get("ok")),
        "fastreact_ok": bool(fastreact.get("ok")),
        "fastreact_pska_tools_loaded": bool(fastreact.get("pska_tools_loaded")),
        "counts": {
            "source_items": int(index.get("source_items") or 0),
            "chunks": int(index.get("chunks") or 0),
            "entities": int(index.get("entities") or 0),
            "hyperedges": int(index.get("hyperedges") or 0),
            "review_items": int(index.get("review_items") or 0),
            "jobs": int(index.get("jobs") or 0),
        },
        "connectors": {
            "source_channels": sorted((connectors.get("source_channels") or {}).keys()),
            "state_count": int(connectors.get("state_count") or 0),
            "enabled_state_count": int(connectors.get("enabled_state_count") or 0),
            "state_sync_status": connectors.get("state_sync_status") or {},
        },
        "jobs": {
            "by_status": jobs.get("by_status") or {},
            "digest_backlog": jobs.get("digest_backlog") or {},
            "running_stale_count": int(jobs.get("running_stale_count") or 0),
        },
        "pending_review_items": int(payload.get("pending_review_items") or 0),
        "next_actions": payload.get("next_actions") or [],
    }


def _daily_status_payload(database_url: str, *, owner_user_id: str = "user_primary", limit: int = 5) -> dict[str, Any]:
    limit = max(0, limit)
    status = _mvp_status_payload(database_url)
    summary = _mvp_status_summary(status)
    jobs = status.get("jobs") or {}
    metrics = status.get("metrics") or {}
    index = metrics.get("index") or {}
    checks = ((status.get("ready") or {}).get("checks") or {})

    try:
        review_items = PSKAApi(database_url).store.list_review_items()
    except Exception:
        review_items = []
    pending_reviews = _review_items_payload(
        review_items,
        status="pending",
        owner_user_id=owner_user_id,
        limit=limit,
        summary=True,
    )
    failed_jobs = list(jobs.get("recent_failed") or [])[:limit]
    recommended_commands = _daily_status_recommended_commands(
        summary=summary,
        pending_review_count=int(pending_reviews.get("total_matching") or 0),
        failed_job_count=int((jobs.get("by_status") or {}).get("failed") or len(failed_jobs)),
    )

    return {
        "ok": bool(summary.get("database_ok")) and bool(summary.get("schema_ok")) and bool(summary.get("mcp_ok")),
        "database_url": database_url,
        "owner_user_id": owner_user_id,
        "requires_fastreact_online": False,
        "service_readiness": {
            "database_ok": bool(summary.get("database_ok")),
            "schema_ok": bool(summary.get("schema_ok")),
            "mcp_ok": bool(summary.get("mcp_ok")),
            "jobs_ok": bool((checks.get("jobs") or {}).get("ok")),
            "metrics_ok": bool((checks.get("metrics") or {}).get("ok")),
            "fastreact_ok": bool(summary.get("fastreact_ok")),
            "fastreact_optional_for_daily_status": True,
        },
        "source_counts": {
            "source_items": int(index.get("source_items") or 0),
            "chunks": int(index.get("chunks") or 0),
        },
        "digest_backlog": (jobs.get("digest_backlog") or {}),
        "pending_reviews": pending_reviews,
        "failed_jobs": {
            "count": int((jobs.get("by_status") or {}).get("failed") or len(failed_jobs)),
            "recent": failed_jobs,
        },
        "recommended_commands": recommended_commands,
        "next_actions": status.get("next_actions") or [],
    }


def _daily_status_recommended_commands(
    *,
    summary: dict[str, Any],
    pending_review_count: int,
    failed_job_count: int,
) -> list[str]:
    commands = ["./scripts/pska daily-status", "./scripts/pska mvp-status --summary"]
    counts = summary.get("counts") or {}
    digest_backlog = ((summary.get("jobs") or {}).get("digest_backlog") or {}).get("jobs") or 0
    if int(counts.get("source_items") or 0) == 0:
        commands.append("./scripts/pska mvp-bootstrap")
    if int(counts.get("source_items") or 0) > 0 and int(counts.get("entities") or 0) == 0:
        commands.append("./scripts/pska extract-all --owner-user-id user_primary")
    if digest_backlog:
        commands.append("./scripts/pska fastreact-digest-worker-command")
    elif int(counts.get("source_items") or 0) > 0:
        commands.append("./scripts/pska digest-schedule --owner-user-id user_primary")
    if pending_review_count:
        commands.append("./scripts/pska review-list --status pending --owner-user-id user_primary --summary")
    if failed_job_count:
        commands.append("./scripts/pska jobs list --status failed")
    return commands


def _fastreact_digest_worker_command_payload(args: argparse.Namespace, config: PSKAConfig) -> dict[str, Any]:
    pska_url = str(args.pska_url or f"http://{config.service.host}:{config.service.port}").rstrip("/")
    fastreact_url = str(args.fastreact_url or config.fastreact.url).rstrip("/")
    fastreact_root = args.fastreact_root.expanduser()
    command = [
        str(args.python),
        "scripts/pska_digest_worker.py",
        "--pska-url",
        pska_url,
        "--fastreact-url",
        fastreact_url,
        "--batch-limit",
        str(args.batch_limit),
        "--represented-user-id",
        str(args.represented_user_id),
    ]
    return {
        "ok": True,
        "pska_database_url": config.database.url,
        "pska_url": pska_url,
        "fastreact_url": fastreact_url,
        "fastreact_root": str(fastreact_root),
        "command": command,
        "shell": f"cd {shlex_quote(str(fastreact_root))} && {' '.join(shlex_quote(part) for part in command)}",
        "notes": [
            "Start PSKA service first with ./scripts/pska --config .pska/config.json local-daemon or serve.",
            "Run ./scripts/pska --config .pska/config.json service-check before starting the worker.",
            "The worker belongs to Fastreact and must use PSKA HTTP API/MCP; it must not access the PSKA database directly.",
        ],
    }


def _mvp_next_actions(payload: dict[str, Any], *, connectors: dict[str, Any]) -> list[str]:
    actions = []
    ready = payload.get("ready") or {}
    checks = ready.get("checks") or {}
    metrics = payload.get("metrics") or {}
    index = metrics.get("index") or {}
    jobs = payload.get("jobs") or {}
    source_items = int(index.get("source_items") or 0)
    if not ready.get("ok"):
        actions.append("Run ./scripts/pska db-init, then ./scripts/pska service-check after starting local-daemon.")
    if source_items == 0:
        actions.append("Run ./scripts/pska mvp-bootstrap to import Twitter/X archive or scan a notes root.")
    if not connectors.get("state_count"):
        actions.append("Authorize a local notes root with ./scripts/pska files-sync or ./scripts/pska files-scan --root <path>.")
    entities = int(index.get("entities") or 0)
    hyperedges = int(index.get("hyperedges") or 0)
    if source_items and (entities == 0 or hyperedges == 0):
        actions.append("Run ./scripts/pska mvp-bootstrap --extract or ./scripts/pska extract-all --owner-user-id user_primary to build the initial graph.")
    digest_backlog = (jobs.get("digest_backlog") or {}).get("jobs") or 0
    if source_items and digest_backlog == 0 and (entities == 0 or hyperedges == 0):
        actions.append("Run ./scripts/pska digest-schedule --owner-user-id user_primary to queue digest work.")
    if digest_backlog and checks.get("fastreact", {}).get("ok") is True:
        actions.append("Run the Fastreact PSKA digest worker to consume queued digest jobs.")
    if checks.get("fastreact", {}).get("ok") is False:
        actions.append("Start Fastreact when you want agentic digest or Fastreact-backed QA.")
    if payload.get("pending_review_items"):
        actions.append("Inspect pending candidates with ./scripts/pska review-list --status pending --summary, then use review-approve or review-reject.")
    if not actions:
        actions.append("System is ready for MVP use: run search, agentic-search, or keep local-daemon running.")
    return actions


def _job_stats(store: PostgresKnowledgeStore, *, limit: int = 1000) -> dict[str, Any]:
    jobs = store.list_jobs(limit=limit)
    by_status = {status: 0 for status in ["queued", "running", "failed", "succeeded", "canceled"]}
    by_type: dict[str, int] = {}
    stale_running = []
    digest_backlog_jobs = 0
    digest_backlog_source_items: set[str] = set()
    for job in jobs:
        by_status[job.status] = by_status.get(job.status, 0) + 1
        by_type[job.job_type] = by_type.get(job.job_type, 0) + 1
        if job.job_type == "digest_via_fastreact" and job.status in {"queued", "running"}:
            digest_backlog_jobs += 1
            digest_backlog_source_items.update(_job_source_item_ids(job))
        if job.status == "running" and job.leased_until and job.leased_until < utc_now():
            stale_running.append({"job_id": job.job_id, "job_type": job.job_type, "worker_id": job.worker_id})
    return {
        "sample_size": len(jobs),
        "by_status": by_status,
        "by_type": by_type,
        "running_stale_count": len(stale_running),
        "stale_running": stale_running[:10],
        "digest_backlog": {
            "jobs": digest_backlog_jobs,
            "source_items": len(digest_backlog_source_items),
        },
    }


def _job_source_item_ids(job) -> set[str]:
    ids = {ref.source_item_id for ref in job.source_refs if ref.source_item_id}
    payload = job.payload if isinstance(job.payload, dict) else {}
    raw_ids = payload.get("source_item_ids")
    if isinstance(raw_ids, list):
        ids.update(str(item) for item in raw_ids if item)
    scope = payload.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("source_item_ids"), list):
        ids.update(str(item) for item in scope["source_item_ids"] if item)
    raw_refs = payload.get("source_refs")
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            if isinstance(ref, dict) and ref.get("source_item_id"):
                ids.add(str(ref["source_item_id"]))
    return ids


def _digest_schedule_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "owner_user_id": args.owner_user_id,
        "source_item_ids": getattr(args, "source_item_ids", []),
        "limit": args.limit,
        "batch_size": args.batch_size,
        "priority": args.priority,
        "max_attempts": args.max_attempts,
        "retry_backoff_seconds": args.retry_backoff_seconds,
        "quota_window_seconds": getattr(args, "quota_window_seconds", 0),
        "max_jobs_per_window": getattr(args, "max_jobs_per_window", 0),
        "force": args.force,
    }
    if args.reason:
        payload["reason"] = args.reason
    return payload


def _service_check_headers(service_token: str | None) -> dict[str, str]:
    headers = {"accept": "application/json"}
    if service_token:
        headers["X-PSKA-Service-Token"] = service_token
    return headers


def _service_check_request(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = dict(headers)
    if data is not None:
        request_headers["content-type"] = "application/json; charset=utf-8"
    request = Request(f"{base_url}{path}", data=data, method=method, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body or "{}")
            return {"ok": 200 <= response.status < 300, "status": response.status, "payload": parsed}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": body}
    except (URLError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc)}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}"}


if __name__ == "__main__":
    raise SystemExit(main())
