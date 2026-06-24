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
from pska_core.agent_capture import capture_agent_conversation
from pska_core.agentic_service import AgenticServiceError, build_agentic_service_client
from pska_core.api import PSKAApi, serve
from pska_core.candidates import CandidateWriteService
from pska_core.config import DEFAULT_DATABASE_URL, PSKAConfig, expand_path
from pska_core.connectors import connector_state_from_mapping, connector_record_to_payload
from pska_core.discovery import DiscoveryService
from pska_core.embeddings import EmbeddingConfig, EmbeddingService, build_embedding_provider
from pska_core.enums import ReviewType, Visibility
from pska_core.extraction import ExtractionService
from pska_core.fastreact_client import FastreactConfig, FastreactError, HttpFastreactClient
from pska_core.files_connector import scan_files
from pska_core.files_watcher import watch_files
from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JOB_TYPES, JobService
from pska_core.knowledge_sources import KnowledgeSourceService
from pska_core.local_daemon import build_process_specs, config_check, daemon_status, run_supervisor, supervisor_config
from pska_core.memory import MemoryService
from pska_core.mcp_server import MCPServer
from pska_core.models import AgentMemory, ChannelIngestPayload, ReviewItem, SourceItem, SourceRef, UserProfileCard, utc_now
from pska_core.retrieval import RetrievalService
from pska_core.retrieval_eval import DEFAULT_RETRIEVAL_EVAL_FIXTURE, run_retrieval_eval
from pska_core.review import ReviewService
from pska_core.serde import dumps, to_jsonable
from pska_core.store_postgres import PostgresKnowledgeStore


SMOKE_DATABASE_URL = "postgresql:///pska_smoke"
REPO_ROOT = Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pska-core", description="PSKA Core local utilities")
    parser.add_argument("--config", type=Path, default=None, help="Path to PSKA JSON config")
    parser.add_argument("--workspace-root", type=Path, default=None, help="PSKA local workspace root")
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
    import_parser.add_argument("--input", type=Path, default=None)
    import_parser.add_argument("--archive-root", type=Path, default=None)
    import_parser.add_argument("--owner-user-id", default="user_primary")
    import_parser.add_argument("--space-id", default="private_primary")
    import_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=Visibility.PRIVATE.value)
    import_parser.add_argument("--visible-team-ids", default="")
    _add_embedding_args(import_parser, default_provider="disabled")

    search_parser = subparsers.add_parser("search", help="Search PSKA Core")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--user-id", default="user_primary")
    search_parser.add_argument("--represented-user-id", default=None)
    search_parser.add_argument("--top-k", type=int, default=5)
    _add_embedding_args(search_parser, default_provider="disabled")

    agentic_parser = subparsers.add_parser("agentic-search", help="Run agentic PSKA search")
    agentic_parser.add_argument("--query", required=True)
    agentic_parser.add_argument("--user-id", default="user_primary")
    agentic_parser.add_argument("--represented-user-id", default=None)
    agentic_parser.add_argument("--capture", action="store_true", help="Save the agentic answer, citations, and trace as PSKA source material")
    _add_embedding_args(agentic_parser, default_provider="disabled")

    embed_parser = subparsers.add_parser("embed-backfill", help="Backfill missing chunk embeddings")
    _add_embedding_args(embed_parser, default_provider="bge-m3")
    embed_parser.add_argument("--batch-size", type=int, default=None)
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

    knowledge_source_parser = subparsers.add_parser("knowledge-source", help="Manage user-facing Knowledge Sources")
    knowledge_source_parser.add_argument("action", choices=["list", "add-folder"], nargs="?", default="list")
    knowledge_source_parser.add_argument("--path", type=Path, default=None)
    knowledge_source_parser.add_argument("--name", default=None)
    knowledge_source_parser.add_argument("--owner-user-id", default="user_primary")
    knowledge_source_parser.add_argument("--space-id", default="private_primary")
    knowledge_source_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=Visibility.PRIVATE.value)
    knowledge_source_parser.add_argument("--mode", choices=["manual", "watching", "paused"], default="manual")
    knowledge_source_parser.add_argument("--ignore", action="append", default=[])
    knowledge_source_parser.add_argument("--max-bytes", type=int, default=1_000_000)

    files_scan_parser = subparsers.add_parser("files-scan", help="Scan an authorized local directory through the Files connector")
    files_scan_parser.add_argument("--root", type=Path, required=True)
    files_scan_parser.add_argument("--owner-user-id", default="user_primary")
    files_scan_parser.add_argument("--space-id", default="private_primary")
    files_scan_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=Visibility.PRIVATE.value)
    files_scan_parser.add_argument("--visible-team-ids", default="")
    files_scan_parser.add_argument("--ignore", action="append", default=[])
    files_scan_parser.add_argument("--max-bytes", type=int, default=1_000_000)
    _add_embedding_args(files_scan_parser, default_provider="disabled")

    files_sync_parser = subparsers.add_parser("files-sync", help="Scan configured Files connector roots from PSKA config")
    files_sync_parser.add_argument("--root", type=Path, action="append", default=[], help="Additional or override root to scan")
    files_sync_parser.add_argument("--owner-user-id", default=None)
    files_sync_parser.add_argument("--space-id", default=None)
    files_sync_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=None)
    files_sync_parser.add_argument("--ignore", action="append", default=[])
    files_sync_parser.add_argument("--max-bytes", type=int, default=None)
    files_sync_parser.add_argument("--twitter-archive", type=Path, default=None, help="Twitter/X zip inbox to import during files sync")
    files_sync_parser.add_argument("--archive-root", type=Path, default=None, help="Archive extraction root for imported Twitter/X zips")
    files_sync_parser.add_argument("--skip-twitter-archives", action="store_true")
    _add_embedding_args(files_sync_parser, default_provider="disabled")

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
    _add_embedding_args(files_watch_parser, default_provider="disabled")

    extract_parser = subparsers.add_parser("extract-all", help="Extract entities/hyperedges from source items")
    extract_parser.add_argument("--owner-user-id", default=None)

    serve_parser = subparsers.add_parser("serve", help="Start local PSKA Core HTTP API")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    local_daemon_parser = subparsers.add_parser("local-daemon", help="Run or inspect the local PSKA service supervisor")
    local_daemon_parser.add_argument("action", choices=["run", "status", "config-check", "supervisor-config"], nargs="?", default="run")
    local_daemon_parser.add_argument("--no-worker", action="store_true")
    local_daemon_parser.add_argument("--no-digest-scheduler", action="store_true")
    local_daemon_parser.add_argument("--restart", action="store_true", help="Restart child processes if they exit")
    local_daemon_parser.add_argument("--run-dir", type=Path, default=None)
    local_daemon_parser.add_argument("--log-dir", type=Path, default=None)
    local_daemon_parser.add_argument("--supervisor", choices=["supervisord", "launchd"], default="supervisord")
    local_daemon_parser.add_argument("--dry-run", action="store_true", help="For supervisor-config, print config without installing it")
    local_daemon_parser.add_argument("--worker-id", default="pska-worker-local")
    local_daemon_parser.add_argument("--poll-interval", type=float, default=5.0)
    local_daemon_parser.add_argument("--lease-seconds", type=int, default=300)
    local_daemon_parser.add_argument("--recover-stale-seconds", type=int, default=900)
    local_daemon_parser.add_argument("--digest-interval-seconds", type=float, default=300.0)
    local_daemon_parser.add_argument("--digest-limit", type=int, default=20)
    local_daemon_parser.add_argument("--digest-batch-size", type=int, default=1)
    local_daemon_parser.add_argument("--digest-max-backlog-jobs", type=int, default=10)

    mvp_bootstrap_parser = subparsers.add_parser("mvp-bootstrap", help="Initialize the MVP scope: DB, Twitter archive, local text roots, and digest backlog")
    mvp_bootstrap_parser.add_argument("--twitter-archive", type=Path, default=None)
    mvp_bootstrap_parser.add_argument("--notes-root", type=Path, action="append", default=[])
    mvp_bootstrap_parser.add_argument("--archive-root", type=Path, default=None)
    mvp_bootstrap_parser.add_argument("--owner-user-id", default="user_primary")
    mvp_bootstrap_parser.add_argument("--space-id", default="private_primary")
    mvp_bootstrap_parser.add_argument("--skip-twitter", action="store_true")
    mvp_bootstrap_parser.add_argument("--skip-files", action="store_true")
    mvp_bootstrap_parser.add_argument("--skip-digest", action="store_true")
    mvp_bootstrap_parser.add_argument("--extract", action="store_true", help="Run initial LLM extraction after ingesting MVP sources")
    mvp_bootstrap_parser.add_argument("--digest-limit", type=int, default=20)
    mvp_bootstrap_parser.add_argument("--digest-batch-size", type=int, default=1)
    mvp_bootstrap_parser.add_argument("--dry-run", action="store_true")
    _add_embedding_args(mvp_bootstrap_parser, default_provider="disabled")

    mvp_status_parser = subparsers.add_parser("mvp-status", help="Show MVP readiness, metrics, and next actions")
    mvp_status_parser.add_argument("--summary", action="store_true", help="Print a compact human-scale MVP status summary")

    daily_status_parser = subparsers.add_parser("daily-status", help="Show deterministic daily PSKA readiness, backlog, and next commands")
    daily_status_parser.add_argument("--owner-user-id", default="user_primary")
    daily_status_parser.add_argument("--limit", type=int, default=5, help="Maximum pending reviews and failed jobs to include")

    daily_briefing_parser = subparsers.add_parser("daily-briefing", help="Show deterministic daily PSKA briefing and next actions")
    daily_briefing_parser.add_argument("--owner-user-id", default="user_primary")
    daily_briefing_parser.add_argument("--limit", type=int, default=5, help="Maximum source/review/job rows to include")
    daily_briefing_parser.add_argument("--narrative", action="store_true", help="Ask FastReAct for a narrative summary and save it when available")
    daily_briefing_parser.add_argument("--narrative-timeout-seconds", type=float, default=None, help="Override FastReAct chat timeout for --narrative")

    ops_briefing_parser = subparsers.add_parser("ops-briefing", help="Show deterministic human-readable PSKA ops diagnostics")
    ops_briefing_parser.add_argument("--owner-user-id", default="user_primary")
    ops_briefing_parser.add_argument("--limit", type=int, default=5, help="Maximum failed/stale rows to include")
    ops_briefing_parser.add_argument("--connector-stale-seconds", type=int, default=86_400)
    ops_briefing_parser.add_argument("--format", choices=["json", "text"], default="json")

    retrieval_eval_parser = subparsers.add_parser("retrieval-eval", help="Run retrieval/GraphRAG eval fixture")
    retrieval_eval_parser.add_argument("--fixture", type=Path, default=DEFAULT_RETRIEVAL_EVAL_FIXTURE)
    retrieval_eval_parser.add_argument("--real", action="store_true", help="Use real embedding model and LLM-backed agentic search")
    _add_embedding_args(retrieval_eval_parser, default_provider="disabled")

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
    smoke_parser.add_argument("--input", type=Path, default=None)
    smoke_parser.add_argument("--archive-root", type=Path, default=None)
    smoke_parser.add_argument("--query", default="")
    _add_embedding_args(smoke_parser, default_provider="disabled")

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
    run_parser.add_argument(
        "--exclude-job-type",
        action="append",
        dest="excluded_job_types",
        default=[],
        choices=sorted(JOB_TYPES),
        help="Do not claim this job type; repeat to exclude multiple types",
    )

    worker_parser = subparsers.add_parser("job-worker", help="Continuously poll and run durable jobs")
    worker_parser.add_argument("--poll-interval", type=float, default=5.0)
    worker_parser.add_argument("--max-jobs", type=int, default=0, help="Stop after this many jobs; 0 means no limit")
    worker_parser.add_argument("--idle-limit", type=int, default=0, help="Stop after this many idle polls; 0 means no limit")
    worker_parser.add_argument("--recover-stale-seconds", type=int, default=0)
    worker_parser.add_argument("--worker-id", default=None)
    worker_parser.add_argument("--lease-seconds", type=int, default=300)
    worker_parser.add_argument(
        "--exclude-job-type",
        action="append",
        dest="excluded_job_types",
        default=[],
        choices=sorted(JOB_TYPES),
        help="Do not claim this job type; repeat to exclude multiple types",
    )

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
    digest_schedule_parser.add_argument("--batch-size", type=int, default=1)
    digest_schedule_parser.add_argument("--priority", type=int, default=0)
    digest_schedule_parser.add_argument("--max-attempts", type=int, default=3)
    digest_schedule_parser.add_argument("--retry-backoff-seconds", type=int, default=60)
    digest_schedule_parser.add_argument("--quota-window-seconds", type=int, default=0, help="Optional scheduling quota window; 0 disables quota")
    digest_schedule_parser.add_argument("--max-jobs-per-window", type=int, default=0, help="Optional max digest jobs per quota window; 0 disables quota")
    digest_schedule_parser.add_argument("--force", action="store_true")
    digest_schedule_parser.add_argument("--reason", default="")

    digest_now_parser = subparsers.add_parser("digest-now", help="Sync files, schedule digest work, run the Fastreact digest worker, and print a summary")
    digest_now_parser.add_argument("--owner-user-id", default="user_primary")
    digest_now_parser.add_argument("--source-item-id", action="append", dest="source_item_ids", default=[])
    digest_now_parser.add_argument("--limit", type=int, default=20)
    digest_now_parser.add_argument("--batch-size", type=int, default=1)
    digest_now_parser.add_argument("--priority", type=int, default=0)
    digest_now_parser.add_argument("--max-attempts", type=int, default=3)
    digest_now_parser.add_argument("--retry-backoff-seconds", type=int, default=60)
    digest_now_parser.add_argument("--force", action="store_true")
    digest_now_parser.add_argument("--reason", default="manual digest-now")
    digest_now_parser.add_argument("--skip-sync", action="store_true")
    digest_now_parser.add_argument("--root", type=Path, action="append", default=[], help="Additional or override folder source to sync before digest")
    digest_now_parser.add_argument("--space-id", default=None)
    digest_now_parser.add_argument("--visibility", choices=[item.value for item in Visibility], default=None)
    digest_now_parser.add_argument("--ignore", action="append", default=[])
    digest_now_parser.add_argument("--max-bytes", type=int, default=None)
    digest_now_parser.add_argument("--twitter-archive", type=Path, default=None, help="Twitter/X zip inbox to import before digest")
    digest_now_parser.add_argument("--archive-root", type=Path, default=None, help="Archive extraction root for imported Twitter/X zips")
    digest_now_parser.add_argument("--skip-twitter-archives", action="store_true")
    digest_now_parser.add_argument("--fastreact-root", type=Path, default=Path.home() / "Fastreact" / "fastreact-nano")
    digest_now_parser.add_argument("--python", default="python3")
    digest_now_parser.add_argument("--pska-url", default=None)
    digest_now_parser.add_argument("--fastreact-url", default=None)
    digest_now_parser.add_argument("--max-worker-runs", type=int, default=10)
    _add_embedding_args(digest_now_parser, default_provider="disabled")

    digest_scheduler_parser = subparsers.add_parser("digest-scheduler", help="Foreground periodic digest backlog scheduler")
    digest_scheduler_parser.add_argument("--owner-user-id", default="user_primary")
    digest_scheduler_parser.add_argument("--interval-seconds", type=float, default=60.0)
    digest_scheduler_parser.add_argument("--max-cycles", type=int, default=0, help="Stop after this many scheduler cycles; 0 means no limit")
    digest_scheduler_parser.add_argument("--idle-limit", type=int, default=0, help="Stop after this many idle cycles; 0 means no limit")
    digest_scheduler_parser.add_argument("--limit", type=int, default=20)
    digest_scheduler_parser.add_argument("--batch-size", type=int, default=1)
    digest_scheduler_parser.add_argument("--priority", type=int, default=0)
    digest_scheduler_parser.add_argument("--max-attempts", type=int, default=3)
    digest_scheduler_parser.add_argument("--retry-backoff-seconds", type=int, default=60)
    digest_scheduler_parser.add_argument("--quota-window-seconds", type=int, default=0, help="Optional scheduling quota window; 0 disables quota")
    digest_scheduler_parser.add_argument("--max-jobs-per-window", type=int, default=0, help="Optional max digest jobs per quota window; 0 disables quota")
    digest_scheduler_parser.add_argument("--max-backlog-jobs", type=int, default=10)
    digest_scheduler_parser.add_argument("--recover-stale-seconds", type=int, default=0)
    digest_scheduler_parser.add_argument("--force", action="store_true")
    digest_scheduler_parser.add_argument("--reason", default="periodic digest scheduler")

    seed_candidates_parser = subparsers.add_parser(
        "seed-review-candidates",
        help="Create grounded review/discovery candidates from current corpus for cold-start validation",
    )
    seed_candidates_parser.add_argument("--owner-user-id", default="user_primary")
    seed_candidates_parser.add_argument("--limit", type=int, default=4)
    seed_candidates_parser.add_argument("--confidence", type=float, default=0.55)

    review_list_parser = subparsers.add_parser("review-list", help="List review items awaiting or recording human decisions")
    review_list_parser.add_argument("--status", default=None)
    review_list_parser.add_argument("--owner-user-id", default=None)
    review_list_parser.add_argument("--limit", type=int, default=50)
    review_list_parser.add_argument("--summary", action="store_true", help="Print compact review item rows")

    memory_list_parser = subparsers.add_parser("memory-list", help="List user-owned agent memories without modifying them")
    memory_list_parser.add_argument("--owner-user-id", default="user_primary")
    memory_list_parser.add_argument("--limit", type=int, default=50)

    profile_list_parser = subparsers.add_parser("profile-list", help="List user profile cards without modifying them")
    profile_list_parser.add_argument("--owner-user-id", default="user_primary")
    profile_list_parser.add_argument("--limit", type=int, default=50)

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

    review_batch_parser = subparsers.add_parser("review-batch", help="Dry-run or execute safe batch review operations")
    review_batch_parser.add_argument("action", choices=["approve", "reject", "apply"])
    review_batch_parser.add_argument("--review-item-id", action="append", dest="review_item_ids", default=[])
    review_batch_parser.add_argument("--owner-user-id", default=None)
    review_batch_parser.add_argument("--review-type", choices=[item.value for item in ReviewType], default=None)
    review_batch_parser.add_argument("--status", default=None)
    review_batch_parser.add_argument("--limit", type=int, default=50)
    review_batch_parser.add_argument("--actor-user-id", default="user_primary")
    review_batch_parser.add_argument("--reason", default="")
    review_batch_parser.add_argument("--execute", action="store_true", help="Write changes; default is dry-run")

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
    workspace_root = _resolve_workspace_root(args, config)
    args.pska_config = config
    args.database_url = args.database_url or config.database.url
    _apply_workspace_defaults(args, workspace_root)
    if args.command == "service-check":
        args.url = args.url or f"http://{config.service.host}:{config.service.port}"
        args.service_token = args.service_token or config.service.service_token
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
    if args.command == "knowledge-source":
        return knowledge_source(args)
    if args.command == "files-scan":
        return files_scan(args)
    if args.command == "files-sync":
        return files_sync(args, config)
    if args.command == "files-watch":
        return files_watch(args, config)
    if args.command == "extract-all":
        return extract_all(args)
    if args.command == "serve":
        serve(args.host or config.service.host, args.port or config.service.port, args.database_url, config=config)
        return 0
    if args.command == "local-daemon":
        return local_daemon(args, config)
    if args.command == "mvp-bootstrap":
        return mvp_bootstrap(args)
    if args.command == "mvp-status":
        return mvp_status(args)
    if args.command == "daily-status":
        return daily_status(args)
    if args.command == "daily-briefing":
        return daily_briefing(args)
    if args.command == "ops-briefing":
        return ops_briefing(args)
    if args.command == "retrieval-eval":
        return retrieval_eval(args)
    if args.command == "fastreact-digest-worker-command":
        return fastreact_digest_worker_command(args, config)
    if args.command == "service-check":
        return service_check(args)
    if args.command == "mcp-server":
        return MCPServer(args.database_url, config=config).run()
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
    if args.command == "digest-now":
        return digest_now(args, config)
    if args.command == "digest-scheduler":
        return digest_scheduler(args)
    if args.command == "seed-review-candidates":
        return seed_review_candidates(args)
    if args.command == "review-list":
        return review_list(args)
    if args.command == "memory-list":
        return memory_list(args)
    if args.command == "profile-list":
        return profile_list(args)
    if args.command == "review-approve":
        return review_approve(args)
    if args.command == "review-reject":
        return review_reject(args)
    if args.command == "review-apply":
        return review_apply(args)
    if args.command == "review-batch":
        return review_batch(args)
    if args.command == "profile-propose":
        return profile_propose(args)
    return 2


def _resolve_workspace_root(args: argparse.Namespace, config: PSKAConfig) -> Path:
    if args.workspace_root:
        return expand_path(args.workspace_root)
    return expand_path(config.workspace.root)


def _apply_workspace_defaults(args: argparse.Namespace, workspace_root: Path) -> None:
    if args.command in {"import-twitter-zips", "mvp-bootstrap", "smoke-twitter-import", "files-sync", "digest-now"}:
        if getattr(args, "input", None) is None:
            args.input = workspace_root / "twitter_archive"
        if getattr(args, "twitter_archive", None) is None:
            args.twitter_archive = workspace_root / "twitter_archive"
        if getattr(args, "archive_root", None) is None:
            args.archive_root = workspace_root / "imports"
    if args.command == "local-daemon":
        if args.run_dir is None:
            args.run_dir = workspace_root / "run"
        if args.log_dir is None:
            args.log_dir = workspace_root / "logs"


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
    pska_config = getattr(args, "pska_config", None)
    try:
        response = build_agentic_service_client(
            pska_config.agentic_service_runtime_config() if pska_config else None
        ).search(
            args.query,
            user,
            represented_user_id=args.represented_user_id,
        )
    except AgenticServiceError as exc:
        print(f"Agentic service unavailable: {exc}", file=sys.stderr)
        return 2
    if args.capture:
        retrieval = response.get("retrieval") if isinstance(response.get("retrieval"), dict) else {}
        captured = capture_agent_conversation(
            store,
            owner_user_id=args.represented_user_id or args.user_id,
            represented_user_id=args.represented_user_id or args.user_id,
            purpose="agentic_search",
            prompt=args.query,
            answer=str(response.get("answer") or ""),
            source_refs=retrieval.get("citations") or response.get("source_refs") or [],
            trace_summary=response.get("trace") if isinstance(response.get("trace"), dict) else {},
            title=f"PSKA agentic search: {args.query[:80]}",
            source_channel="pska_agent",
        )
        print(
            dumps(
                {
                    "agentic_search": response,
                    "capture": {
                        "action": captured.action,
                        "explanation": captured.explanation,
                        "source_item_id": captured.source_item_id,
                        "review_item_id": captured.review_item_id,
                        "policy": captured.policy,
                    },
                }
            )
        )
    else:
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


def knowledge_source(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    service = KnowledgeSourceService(store)
    if args.action == "add-folder":
        if not args.path:
            print("knowledge-source add-folder requires --path", file=sys.stderr)
            return 2
        source = service.add_folder_source(
            args.path,
            owner_user_id=args.owner_user_id,
            name=args.name,
            mode=args.mode,
            space_id=args.space_id,
            visibility=Visibility(args.visibility),
            ignore=list(args.ignore or []),
            max_bytes=args.max_bytes,
        )
        print(dumps({"knowledge_source": source}))
        return 0
    sources = service.list_sources(owner_user_id=args.owner_user_id)
    runs = {
        source.knowledge_source_id: store.list_sync_runs(knowledge_source_id=source.knowledge_source_id, limit=1)
        for source in sources
    }
    print(dumps({"knowledge_sources": sources, "latest_sync_runs": runs}))
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
    payload = _files_sync_payload(args, config)
    print(dumps(payload))
    return 0 if payload["ok"] else 1


def _files_sync_payload(args: argparse.Namespace, config: PSKAConfig) -> dict[str, Any]:
    store = PostgresKnowledgeStore(args.database_url)
    source_service = KnowledgeSourceService(store)
    try:
        seeded = source_service.seed_from_config(config)
        configured_roots = [root.expanduser().resolve() for root in config.files.roots]
        requested_roots = [root.expanduser().resolve() for root in args.root or []]
        for root in args.root or []:
            seeded.append(
                source_service.add_folder_source(
                    root,
                    owner_user_id=args.owner_user_id or config.files.owner_user_id,
                    space_id=args.space_id or config.files.space_id,
                    visibility=Visibility(args.visibility or config.files.visibility),
                    ignore=[*config.files.ignore, *(args.ignore or [])],
                    max_bytes=args.max_bytes or config.files.max_bytes,
                )
            )
        active_uris = {root.as_uri() for root in [*configured_roots, *requested_roots]}
        sources = [
            source
            for source in source_service.list_sources(owner_user_id=args.owner_user_id or config.files.owner_user_id, source_type="folder")
            if source.mode != "paused" and source.status != "paused"
            and source.uri in active_uris
        ]
    except Exception as exc:  # noqa: BLE001 - preserve old no-root behavior when the DB is not reachable.
        if args.root or config.files.roots:
            raise
        return {
            "ok": False,
            "error": "No knowledge sources configured. Add files.roots to .pska/config.json for cold start seed or pass --root <path>.",
            "database_error": f"{type(exc).__name__}: {exc}",
            "reports": [],
            "knowledge_sources": [],
        }
    if not sources:
        return {
            "ok": False,
            "error": "No knowledge sources configured. Add files.roots to .pska/config.json for cold start seed or pass --root <path>.",
            "reports": [],
            "knowledge_sources": [],
        }
    reports = []
    sync_runs = []
    failed = []
    for source in sources:
        root = source_service.source_path(source)
        try:
            report = scan_files(
                store,
                root=root,
                owner_user_id=source.owner_user_id,
                space_id=source.space_id,
                visibility=source.visibility,
                visible_team_ids=source.visible_team_ids,
                ignore=[*list(source.config.get("ignore") or []), *(args.ignore or [])],
                max_bytes=args.max_bytes or int(source.config.get("max_bytes") or config.files.max_bytes),
                embedding_provider=_embedding_provider_from_args(args),
            )
            reports.append(report)
            failed.extend(report.failed)
            sync_runs.append(source_service.record_sync_report(source, report))
        except Exception as exc:  # noqa: BLE001 - report all roots together.
            error = f"{type(exc).__name__}: {exc}"
            failed.append({"root": str(root), "knowledge_source_id": source.knowledge_source_id, "error": error})
            sync_runs.append(source_service.record_sync_error(source, error))
    twitter_archives = _files_sync_twitter_archives(args, config, store)
    failed.extend(twitter_archives.get("failed") or [])
    payload = {
        "ok": not failed,
        "database_url": args.database_url,
        "seeded_knowledge_sources": seeded,
        "knowledge_sources": sources,
        "roots": [str(source_service.source_path(source).expanduser()) for source in sources],
        "reports": reports,
        "sync_runs": sync_runs,
        "twitter_archives": twitter_archives,
        "totals": {
            "roots": len(sources),
            "scanned": sum(report.scanned for report in reports),
            "ingested": sum(report.ingested for report in reports),
            "twitter_imported": int(twitter_archives.get("imported") or 0),
            "twitter_skipped": int(twitter_archives.get("skipped") or 0),
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
    return payload


def _files_sync_twitter_archives(args: argparse.Namespace, config: PSKAConfig, store: PostgresKnowledgeStore) -> dict[str, Any]:
    if getattr(args, "skip_twitter_archives", False):
        return {"ok": True, "enabled": False, "reason": "skip_twitter_archives", "imported": 0, "skipped": 0, "failed": []}
    input_dir = expand_path(getattr(args, "twitter_archive", None) or config.workspace.twitter_archive_dir)
    archive_root = expand_path(getattr(args, "archive_root", None) or config.workspace.imports_dir)
    if not input_dir.exists():
        return {
            "ok": True,
            "enabled": True,
            "input": str(input_dir),
            "archive_root": str(archive_root),
            "imported": 0,
            "skipped": 0,
            "failed": [],
            "reason": "twitter_archive_directory_not_found",
        }
    importer = TwitterZipImporter(
        store,
        archive_root=archive_root,
        owner_user_id=getattr(args, "owner_user_id", None) or config.files.owner_user_id,
        space_id=getattr(args, "space_id", None) or config.files.space_id,
        visibility=Visibility(getattr(args, "visibility", None) or config.files.visibility),
        visible_team_ids=[],
        embedding_provider=_embedding_provider_from_args(args),
    )
    result = importer.import_directory(input_dir)
    payload = asdict(result)
    return {
        "ok": not payload.get("failed"),
        "enabled": True,
        "input": str(input_dir),
        "archive_root": str(archive_root),
        **payload,
    }


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
    if args.action == "config-check":
        payload = config_check(config, database_url=args.database_url)
        print(dumps(payload))
        return 0 if payload["ok"] else 1
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
    if args.action == "status":
        print(dumps(daemon_status(specs, run_dir=args.run_dir, log_dir=args.log_dir)))
        return 0
    if args.action == "supervisor-config":
        payload = supervisor_config(
            specs,
            supervisor=args.supervisor,
            run_dir=args.run_dir,
            log_dir=args.log_dir,
            working_directory=REPO_ROOT,
        )
        print(dumps(payload))
        return 0
    return run_supervisor(specs, restart=args.restart, run_dir=args.run_dir, log_dir=args.log_dir)


def mvp_bootstrap(args: argparse.Namespace) -> int:
    pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
    report: dict[str, Any] = {
        "database_url": args.database_url,
        "dry_run": bool(args.dry_run),
        "workspace": {
            "root": str(args.workspace_root or pska_config.workspace.root),
            "twitter_archive": str(args.twitter_archive),
            "archive_root": str(args.archive_root),
        },
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
        digest = _build_api(args.database_url, pska_config).schedule_digest(
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
        report["status"] = _mvp_status_payload(args.database_url, pska_config)
        report["ok"] = bool(report["status"].get("ok"))
    except Exception as exc:  # noqa: BLE001 - bootstrap should produce an actionable report.
        report["ok"] = False
        report["status_error"] = f"{type(exc).__name__}: {exc}"
    print(dumps(report))
    return 0


def mvp_status(args: argparse.Namespace) -> int:
    payload = _mvp_status_payload(args.database_url, getattr(args, "pska_config", PSKAConfig.load(args.config)))
    print(dumps(_mvp_status_summary(payload) if args.summary else payload))
    return 0


def daily_status(args: argparse.Namespace) -> int:
    payload = _daily_status_payload(
        args.database_url,
        owner_user_id=args.owner_user_id,
        limit=args.limit,
        pska_config=getattr(args, "pska_config", PSKAConfig.load(args.config)),
        config_path=args.config,
    )
    print(dumps(payload))
    return 0


def daily_briefing(args: argparse.Namespace) -> int:
    pska_config = getattr(args, "pska_config", None)
    payload = _daily_briefing_payload(
        args.database_url,
        owner_user_id=args.owner_user_id,
        limit=args.limit,
        narrative=args.narrative,
        narrative_timeout_seconds=args.narrative_timeout_seconds,
        pska_config=pska_config,
        config_path=args.config,
    )
    print(dumps(payload))
    return 0


def ops_briefing(args: argparse.Namespace) -> int:
    payload = _ops_briefing_payload(
        args.database_url,
        owner_user_id=args.owner_user_id,
        limit=args.limit,
        connector_stale_seconds=args.connector_stale_seconds,
        pska_config=getattr(args, "pska_config", PSKAConfig.load(args.config)),
    )
    print(_ops_briefing_text(payload) if args.format == "text" else dumps(payload))
    return 0


def retrieval_eval(args: argparse.Namespace) -> int:
    embedding_config = None
    if args.real:
        pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
        provider = getattr(args, "embedding_provider", None) or pska_config.embedding.provider or "bge-m3"
        if str(provider).strip().lower() in {"", "disabled", "none", "off"}:
            provider = "bge-m3"
        embedding_config = EmbeddingConfig(
            provider=provider,
            model=getattr(args, "embedding_model", None) or pska_config.embedding.model or "BAAI/bge-m3",
            dimensions=getattr(args, "embedding_dimensions", None) or pska_config.embedding.dimensions or 1024,
            batch_size=getattr(args, "batch_size", None) or pska_config.embedding.batch_size or 16,
        )
    payload = run_retrieval_eval(args.fixture, real=bool(args.real), embedding_config=embedding_config)
    print(dumps(payload))
    return 0 if payload["ok"] else 1


def fastreact_digest_worker_command(args: argparse.Namespace, config: PSKAConfig) -> int:
    payload = _fastreact_digest_worker_command_payload(args, config)
    print(dumps(payload))
    return 0 if payload["ok"] else 1


def job_submit(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    payload = json.loads(args.payload.read_text(encoding="utf-8")) if args.payload else {}
    pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
    service = JobService(store, workspace_root=pska_config.workspace.root)
    job = service.submit(args.job_type, payload, max_attempts=args.max_attempts)
    result: dict[str, object] = {"job": job}
    if args.run_now:
        result["run"] = service.run_available(limit=1)
    print(dumps(result))
    return 0


def job_run(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
    service = JobService(
        store,
        workspace_root=pska_config.workspace.root,
        worker_id=args.worker_id or _default_worker_id(),
        lease_seconds=args.lease_seconds,
        excluded_job_types=set(args.excluded_job_types or []),
    )
    report = service.run_until_empty(limit=args.limit if args.limit > 0 else None) if args.until_empty else service.run_available(limit=args.limit)
    payload = to_jsonable(report)
    diagnostics = _job_run_diagnostics(store, report=payload, excluded_job_types=set(args.excluded_job_types or []))
    if diagnostics:
        payload["diagnostics"] = diagnostics
    print(dumps(payload))
    return 1 if report.failed else 0


def job_worker(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
    service = JobService(
        store,
        workspace_root=pska_config.workspace.root,
        worker_id=args.worker_id or _default_worker_id(),
        lease_seconds=args.lease_seconds,
        excluded_job_types=set(args.excluded_job_types or []),
    )
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


def _job_run_diagnostics(
    store: PostgresKnowledgeStore,
    *,
    report: dict[str, Any],
    excluded_job_types: set[str],
) -> dict[str, Any]:
    if int(report.get("processed") or 0) != 0:
        return {}
    queued_jobs = [
        job
        for job in store.list_jobs(status="queued", limit=20)
        if job.job_type not in excluded_job_types
    ]
    failed_jobs = store.list_jobs(status="failed", limit=5)
    pending_reviews = [item for item in store.list_review_items() if item.status == "pending"]
    diagnostics: dict[str, Any] = {
        "reason": "no_runnable_queued_jobs",
        "queued_jobs": len(queued_jobs),
        "failed_jobs": len(failed_jobs),
        "pending_reviews": len(pending_reviews),
        "next_actions": [],
    }
    if failed_jobs:
        diagnostics["next_actions"].append("./scripts/pska jobs list --status failed")
        diagnostics["next_actions"].append("./scripts/pska job-status --job-id <job_id>")
    if pending_reviews:
        diagnostics["next_actions"].append("./scripts/pska review-list --status pending --owner-user-id user_primary --summary")
    if not queued_jobs:
        diagnostics["next_actions"].append("./scripts/pska digest-schedule --owner-user-id user_primary --force")
    return diagnostics


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
    pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
    jobs = JobService(store, workspace_root=pska_config.workspace.root).recover_stale(max_age_seconds=args.max_age_seconds)
    print(dumps({"recovered": jobs}))
    return 0


def digest_schedule(args: argparse.Namespace) -> int:
    payload = _digest_schedule_payload(args)
    pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
    print(dumps(_build_api(args.database_url, pska_config).schedule_digest(payload)))
    return 0


def digest_now(args: argparse.Namespace, config: PSKAConfig) -> int:
    api = _build_api(args.database_url, config)
    sync_payload = None
    if not args.skip_sync:
        sync_payload = _files_sync_payload(args, config)
        if not sync_payload.get("ok"):
            print(dumps({"ok": False, "stage": "files_sync", "sync": sync_payload}))
            return 1

    scheduled = api.schedule_digest(_digest_schedule_payload(args))
    worker_runs = _run_fastreact_digest_worker(args, config)
    diagnostics = _digest_now_diagnostics(worker_runs)
    fallback_review = _digest_now_fallback_review(
        api.store,
        owner_user_id=args.owner_user_id,
        scheduled_source_item_ids=scheduled.get("scheduled_source_item_ids") or [],
        diagnostics=diagnostics,
        worker_runs=worker_runs,
    )
    stats = api.job_stats()["stats"]
    discoveries = api.workspace_discoveries(owner_user_id=args.owner_user_id, limit=50)
    all_new_discoveries = api.workspace_discoveries(owner_user_id=args.owner_user_id, limit=50, min_score=0)
    pending_reviews = _review_items_payload(
        api.store.list_review_items(),
        status="pending",
        owner_user_id=args.owner_user_id,
        limit=50,
        summary=True,
    )
    failed_digest_jobs = [
        job
        for job in api.store.list_jobs(status="failed", job_type=DIGEST_VIA_FASTREACT, limit=10)
    ]
    candidate_summary = _digest_now_candidate_summary(worker_runs)
    candidate_summary["review_items"] += int(fallback_review.get("review_items") or 0)
    payload = {
        "ok": not any(run.get("ok") is False for run in worker_runs),
        "sync": sync_payload,
        "digest": scheduled,
        "worker_runs": worker_runs,
        "summary": {
            "synced": None if sync_payload is None else sync_payload.get("totals"),
            "scheduled_source_items": len(scheduled.get("scheduled_source_item_ids") or []),
            "worker_processed": sum(int(run.get("processed") or 0) for run in worker_runs if isinstance(run, dict)),
            "candidate_write": candidate_summary,
            "diagnostics": diagnostics,
            "fallback_review": fallback_review,
            "discoveries_visible_count": int(discoveries.get("count") or 0),
            "discoveries_total_new": int(all_new_discoveries.get("total_new") or 0),
            "discoveries_min_score": discoveries.get("min_score"),
            "low_score_discovery_count": max(0, int(all_new_discoveries.get("total_new") or 0) - int(discoveries.get("count") or 0)),
            "digest_backlog": stats.get("digest_backlog") or {},
            "pending_review_count": int(pending_reviews.get("count") or 0),
            "failed_digest_jobs": len(failed_digest_jobs),
        },
        "discoveries": discoveries,
        "low_score_discoveries": all_new_discoveries,
        "pending_reviews": pending_reviews,
        "failed_digest_jobs": failed_digest_jobs,
    }
    print(dumps(payload))
    return 0 if payload["ok"] and not failed_digest_jobs else 1


def _digest_now_fallback_review(
    store,
    *,
    owner_user_id: str,
    scheduled_source_item_ids: Sequence[str],
    diagnostics: dict[str, Any],
    worker_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    warnings = set(diagnostics.get("warnings") or [])
    if "fastreact_digest_completed_without_write_candidates" not in warnings:
        return {"created": False, "reason": "fastreact_wrote_candidates_or_no_processed_run", "review_items": 0}
    processed = sum(int(run.get("processed") or 0) for run in worker_runs if isinstance(run, dict))
    if processed <= 0:
        return {"created": False, "reason": "no_processed_digest_job", "review_items": 0}
    source_ids = [str(item) for item in scheduled_source_item_ids if item]
    if not source_ids:
        return {"created": False, "reason": "no_scheduled_source_items", "review_items": 0}
    known_items = {
        item.source_item_id: item
        for item in store.list_source_items()
        if item.owner_user_id == owner_user_id and item.source_item_id in set(source_ids)
    }
    source_refs = [{"source_item_id": source_id} for source_id in source_ids if source_id in known_items]
    if not source_refs:
        return {"created": False, "reason": "scheduled_source_items_not_found", "review_items": 0}
    titles = [known_items[ref["source_item_id"]].title for ref in source_refs[:3]]
    more = len(source_refs) - len(titles)
    title_suffix = "、".join(title for title in titles if title)
    if more > 0:
        title_suffix = f"{title_suffix} 等 {len(source_refs)} 条" if title_suffix else f"{len(source_refs)} 条来源"
    payload = {
        "schema_version": "pska.candidates.v1",
        "owner_user_id": owner_user_id,
        "producer": "pska_digest_now_fallback",
        "request_id": "digest_now_fallback:" + ":".join(source_ids),
        "source_refs": source_refs,
        "review_items": [
            {
                "review_type": ReviewType.LOW_CONFIDENCE.value,
                "title": f"Digest agent did not write candidates: {title_suffix or source_ids[0]}",
                "proposal": {
                    "reason": "fastreact_digest_completed_without_write_candidates",
                    "message": "FastReact processed the digest job but did not call pska_write_candidates. Review these sources manually or rerun digest after fixing the agent/tool path.",
                    "source_item_ids": source_ids,
                    "diagnostics": diagnostics,
                },
            }
        ],
    }
    summary = CandidateWriteService(store).write_candidates(payload)
    return {"created": True, "reason": "fastreact_digest_completed_without_write_candidates", "review_items": len(summary["review_items"]), "summary": summary}


def _digest_now_candidate_summary(worker_runs: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "entities": 0,
        "hyperedges": 0,
        "knowledge_claims": 0,
        "digest_notes": 0,
        "review_items": 0,
        "memory_candidates": 0,
        "saved_candidates": 0,
        "review_candidates": 0,
        "tool_calls": 0,
    }
    for run in worker_runs:
        for fastreact_run in ((run.get("result") or {}).get("fastreact_runs") or []):
            for tool_call in fastreact_run.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                summary["tool_calls"] += 1
                summary["entities"] += int(tool_call.get("entity_count") or 0)
                summary["hyperedges"] += int(tool_call.get("hyperedge_count") or 0)
                summary["knowledge_claims"] += int(tool_call.get("knowledge_claim_count") or tool_call.get("knowledge_claims") or 0)
                summary["digest_notes"] += int(tool_call.get("digest_note_count") or tool_call.get("digest_notes") or 0)
                summary["review_items"] += int(tool_call.get("review_item_count") or 0)
                summary["memory_candidates"] += int(tool_call.get("memory_candidate_count") or 0)
                summary["saved_candidates"] += int(tool_call.get("saved_candidate_count") or tool_call.get("saved_candidates") or 0)
                summary["review_candidates"] += int(tool_call.get("review_candidate_count") or tool_call.get("review_candidates") or 0)
    return summary


def _digest_now_diagnostics(worker_runs: list[dict[str, Any]]) -> dict[str, Any]:
    write_call_count = 0
    job_context_call_count = 0
    fastreact_run_count = 0
    warnings: list[str] = []
    for run in worker_runs:
        for fastreact_run in ((run.get("result") or {}).get("fastreact_runs") or []):
            if not isinstance(fastreact_run, dict):
                continue
            fastreact_run_count += 1
            write_call_count += int(fastreact_run.get("write_call_count") or 0)
            job_context_call_count += int(fastreact_run.get("job_context_call_count") or 0)
            run_claim_count = 0
            run_digest_note_count = 0
            run_hyperedge_count = 0
            run_write_candidate_count = 0
            for tool_call in fastreact_run.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                tool_name = tool_call.get("tool_name")
                if tool_name == "pska_pska_write_candidates":
                    write_call_count += 1 if "write_call_count" not in fastreact_run else 0
                    run_claim_count += int(tool_call.get("knowledge_claim_count") or tool_call.get("knowledge_claims") or 0)
                    run_digest_note_count += int(tool_call.get("digest_note_count") or tool_call.get("digest_notes") or 0)
                    run_hyperedge_count += int(tool_call.get("hyperedge_count") or 0)
                    for key in [
                        "knowledge_claim_count",
                        "digest_note_count",
                        "entity_count",
                        "hyperedge_count",
                        "review_item_count",
                        "memory_candidate_count",
                    ]:
                        run_write_candidate_count += int(tool_call.get(key) or 0)
                if tool_name == "pska_pska_job_context":
                    job_context_call_count += 1 if "job_context_call_count" not in fastreact_run else 0
            if int(fastreact_run.get("write_call_count") or 0) > 0 and run_write_candidate_count == 0:
                warnings.append("fastreact_write_candidates_called_without_candidates")
            if run_claim_count == 0 and (run_digest_note_count > 0 or run_hyperedge_count > 0):
                warnings.append("fastreact_digest_wrote_digest_or_relationship_without_knowledge_claims")
    processed = sum(int(run.get("processed") or 0) for run in worker_runs if isinstance(run, dict))
    if processed and fastreact_run_count and write_call_count == 0:
        warnings.append("fastreact_digest_completed_without_write_candidates")
    warnings = list(dict.fromkeys(warnings))
    return {
        "fastreact_run_count": fastreact_run_count,
        "write_call_count": write_call_count,
        "job_context_call_count": job_context_call_count,
        "warnings": warnings,
    }


def _run_fastreact_digest_worker(args: argparse.Namespace, config: PSKAConfig) -> list[dict[str, Any]]:
    max_runs = max(1, int(args.max_worker_runs or 1))
    command_args = argparse.Namespace(
        pska_url=args.pska_url,
        fastreact_url=args.fastreact_url,
        fastreact_root=args.fastreact_root,
        python=args.python,
        batch_limit=args.batch_size,
        represented_user_id=args.owner_user_id,
    )
    command_payload = _fastreact_digest_worker_command_payload(command_args, config)
    fastreact_root = Path(command_payload["fastreact_root"])
    command = list(command_payload["command"])
    if not (fastreact_root / "scripts" / "pska_digest_worker.py").exists():
        return [
            {
                "ok": False,
                "stage": "fastreact_worker",
                "error": "fastreact_digest_worker_missing",
                "command": command_payload,
            }
        ]

    runs: list[dict[str, Any]] = []
    for _ in range(max_runs):
        try:
            completed = subprocess.run(  # noqa: S603 - command is assembled from local config/CLI fields.
                command,
                cwd=fastreact_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            runs.append(
                {
                    "ok": False,
                    "stage": "fastreact_worker",
                    "error": "fastreact_digest_worker_timeout",
                    "timeout_seconds": exc.timeout,
                    "command": command,
                }
            )
            break
        run_payload = _parse_json_process_output(completed.stdout)
        if not run_payload:
            run_payload = {
                "ok": completed.returncode == 0,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        if completed.returncode != 0:
            run_payload.setdefault("ok", False)
            run_payload.setdefault("stderr", completed.stderr)
        runs.append(run_payload)
        if int(run_payload.get("processed") or 0) <= 0:
            break
    return runs


def _parse_json_process_output(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_api(database_url: str, config: PSKAConfig | None = None):
    if config is None:
        return PSKAApi(database_url)
    try:
        return PSKAApi(database_url, config=config)
    except TypeError:
        return PSKAApi(database_url)


def digest_scheduler(args: argparse.Namespace) -> int:
    pska_config = getattr(args, "pska_config", PSKAConfig.load(args.config))
    api = _build_api(args.database_url, pska_config)
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


def seed_review_candidates(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    owner_user_id = str(args.owner_user_id)
    limit = max(1, int(args.limit))
    confidence = max(0.0, min(float(args.confidence), 1.0))
    sources = [
        item
        for item in sorted(store.list_source_items(), key=lambda source: source.created_at, reverse=True)
        if item.owner_user_id == owner_user_id
    ][:limit]
    if not sources:
        print(dumps({"ok": False, "error": "no_source_items", "owner_user_id": owner_user_id}))
        return 1

    source_refs = [{"source_item_id": item.source_item_id} for item in sources]
    primary = sources[0]
    secondary = sources[1] if len(sources) > 1 else sources[0]
    payload = {
        "schema_version": "pska.candidates.v1",
        "owner_user_id": owner_user_id,
        "producer": "pska_seed_review_candidates",
        "request_id": "seed_review_candidates:" + ":".join(item.source_item_id for item in sources),
        "source_refs": source_refs,
        "memory_candidates": [
            {
                "title": f"Review memory candidate from {item.title or item.source_item_id}",
                "text": _seed_memory_text(item),
                "confidence": min(confidence, 0.59),
                "source_refs": [{"source_item_id": item.source_item_id}],
            }
            for item in sources
            if _seed_memory_text(item)
        ],
        "hyperedges": [
            {
                "relation_type": "seeded_corpus_relation",
                "title": f"Review relationship candidate: {primary.title or primary.source_item_id} ↔ {secondary.title or secondary.source_item_id}",
                "evidence_text": _seed_relationship_evidence(primary, secondary),
                "confidence": min(confidence, 0.59),
                "source_refs": source_refs[:2],
                "members": [
                    {"entity_type": "source", "label": primary.title or primary.source_item_id, "role": "source_a"},
                    {"entity_type": "source", "label": secondary.title or secondary.source_item_id, "role": "source_b"},
                ],
            }
        ],
    }
    candidate_summary = CandidateWriteService(store).write_candidates(payload)
    discoveries = DiscoveryService(store, owner_user_id=owner_user_id).produce()
    pending_reviews = [
        item
        for item in store.list_review_items()
        if item.owner_user_id == owner_user_id and item.status == "pending"
    ]
    print(
        dumps(
            {
                "ok": True,
                "owner_user_id": owner_user_id,
                "source_items": [item.source_item_id for item in sources],
                "candidate_write": candidate_summary,
                "discoveries_produced": discoveries,
                "pending_reviews": pending_reviews,
            }
        )
    )
    return 0


def _seed_memory_text(source: SourceItem) -> str:
    title = " ".join((source.title or source.source_item_id).split())
    content = " ".join((source.content_text or "").split())
    if content:
        return f"{title}: {content[:320]}"
    return title[:360]


def _seed_relationship_evidence(primary: SourceItem, secondary: SourceItem) -> str:
    primary_title = " ".join((primary.title or primary.source_item_id).split())
    secondary_title = " ".join((secondary.title or secondary.source_item_id).split())
    return f"Cold-start seeded relationship candidate linking two real PSKA sources: {primary_title} / {secondary_title}."


def review_list(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    items = store.list_review_items()
    print(dumps(_review_items_payload(items, status=args.status, owner_user_id=args.owner_user_id, limit=args.limit, summary=args.summary)))
    return 0


def review_batch(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    payload = _review_batch_payload(
        store,
        action=args.action,
        review_item_ids=args.review_item_ids,
        owner_user_id=args.owner_user_id,
        review_type=args.review_type,
        status=args.status,
        limit=args.limit,
        actor_user_id=args.actor_user_id,
        reason=args.reason,
        dry_run=not args.execute,
    )
    print(dumps(payload))
    return 0 if payload["ok"] else 1


BATCH_APPLY_SAFE_REVIEW_TYPES = {"profile_update", "relationship_candidate", "memory_candidate", "low_confidence"}


def _review_batch_payload(
    store,
    *,
    action: str,
    review_item_ids: Sequence[str],
    owner_user_id: str | None,
    review_type: str | None,
    status: str | None,
    limit: int,
    actor_user_id: str,
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    items = _review_batch_candidates(
        store.list_review_items(),
        review_item_ids=review_item_ids,
        owner_user_id=owner_user_id,
        review_type=review_type,
        status=status,
        limit=limit,
    )
    plan = _review_batch_plan(items, action=action)
    results = []
    if not dry_run:
        service = ReviewService(store)
        for row in plan["to_process"]:
            review_item_id = row["review_item_id"]
            before = {event.audit_event_id for event in store.list_audit_events("review_item", review_item_id)}
            try:
                if action == "approve":
                    review_item = service.approve(review_item_id, actor_user_id=actor_user_id, reason=reason)
                elif action == "reject":
                    review_item = service.reject(review_item_id, actor_user_id=actor_user_id, reason=reason)
                elif action == "apply":
                    review_item = service.apply(review_item_id, actor_user_id=actor_user_id, reason=reason)
                else:
                    raise ValueError(f"Unsupported review batch action: {action}")
            except Exception as exc:  # noqa: BLE001 - batch output should explain per-item failures.
                plan["skipped"].append({**row, "reason": f"execution_failed:{type(exc).__name__}", "error": str(exc)})
                continue
            after_events = store.list_audit_events("review_item", review_item_id)
            new_events = [event for event in after_events if event.audit_event_id not in before]
            results.append(
                {
                    "review_item": _review_item_summary(review_item),
                    "audit_events": to_jsonable(new_events),
                }
            )
        plan["to_process"] = [
            row
            for row in plan["to_process"]
            if row["review_item_id"] in {result["review_item"]["review_item_id"] for result in results}
        ]
    return {
        "ok": True,
        "action": action,
        "dry_run": dry_run,
        "filters": {
            "review_item_ids": list(review_item_ids),
            "owner_user_id": owner_user_id,
            "review_type": review_type,
            "status": status,
            "limit": max(0, limit),
        },
        "summary": {
            "selected": len(items),
            "to_process": len(plan["to_process"]),
            "skipped": len(plan["skipped"]),
            "affected": len(results) if not dry_run else 0,
        },
        "affected_ids": [result["review_item"]["review_item_id"] for result in results],
        "to_process": plan["to_process"],
        "skipped": plan["skipped"],
        "results": results,
    }


def _review_batch_candidates(
    items: Sequence[ReviewItem],
    *,
    review_item_ids: Sequence[str],
    owner_user_id: str | None,
    review_type: str | None,
    status: str | None,
    limit: int,
) -> list[ReviewItem]:
    requested = set(review_item_ids)
    filtered = [
        item
        for item in items
        if (not requested or item.review_item_id in requested)
        and (owner_user_id is None or item.owner_user_id == owner_user_id)
        and (review_type is None or _review_type_value(item) == review_type)
        and (status is None or item.status == status)
    ]
    return filtered[: max(0, limit)]


def _review_batch_plan(items: Sequence[ReviewItem], *, action: str) -> dict[str, list[dict[str, Any]]]:
    to_process = []
    skipped = []
    for item in items:
        summary = _review_item_summary(item)
        reason = _review_batch_skip_reason(summary, action=action)
        if reason:
            skipped.append({**summary, "reason": reason})
        else:
            to_process.append(summary)
    if action == "apply" and to_process:
        owners = {item["owner_user_id"] for item in to_process}
        review_types = {item["review_type"] for item in to_process}
        if len(owners) > 1 or len(review_types) > 1:
            skipped.extend(
                {**item, "reason": "batch_apply_requires_same_owner_and_review_type"}
                for item in to_process
            )
            to_process = []
    return {"to_process": to_process, "skipped": skipped}


def _review_batch_skip_reason(summary: dict[str, Any], *, action: str) -> str | None:
    status = summary["status"]
    if action in {"approve", "reject"}:
        return None if status == "pending" else f"status_not_pending:{status}"
    if action != "apply":
        return f"unsupported_batch_action:{action}"
    if status != "approved":
        return f"status_not_approved:{status}"
    if not summary["apply_supported"] or not summary["can_apply_now"]:
        return "apply_not_supported"
    if summary["source_ref_status"] != "present":
        return "missing_source_refs"
    if summary["review_type"] not in BATCH_APPLY_SAFE_REVIEW_TYPES:
        return "batch_apply_requires_single_item_for_review_type"
    return None


def _review_type_value(item: ReviewItem) -> str:
    return item.review_type.value if hasattr(item.review_type, "value") else str(item.review_type)


def memory_list(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    print(dumps(_memory_list_payload(store, owner_user_id=args.owner_user_id, limit=args.limit)))
    return 0


def profile_list(args: argparse.Namespace) -> int:
    store = PostgresKnowledgeStore(args.database_url)
    print(dumps(_profile_list_payload(store, owner_user_id=args.owner_user_id, limit=args.limit)))
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
    apply_supported = review_type in {"profile_update", "share_proposal", "relationship_candidate", "memory_candidate"} or (
        review_type == "low_confidence" and bool(item.proposal.get("memory_candidate") or item.proposal.get("text"))
    )
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


def _memory_list_payload(store, *, owner_user_id: str, limit: int = 50) -> dict[str, Any]:
    memories = sorted(
        store.list_agent_memories(owner_user_id=owner_user_id),
        key=lambda memory: (memory.confidence, memory.last_verified_at.isoformat() if memory.last_verified_at else "", memory.agent_memory_id),
        reverse=True,
    )[: max(0, limit)]
    return {
        "owner_user_id": owner_user_id,
        "agent_memories": [_agent_memory_summary(memory) for memory in memories],
        "count": len(memories),
        "limit": max(0, limit),
        "read_only": True,
    }


def _profile_list_payload(store, *, owner_user_id: str, limit: int = 50) -> dict[str, Any]:
    cards = sorted(
        store.list_profile_cards(owner_user_id=owner_user_id),
        key=lambda card: (card.confidence, card.profile_card_id),
        reverse=True,
    )[: max(0, limit)]
    return {
        "owner_user_id": owner_user_id,
        "profile_cards": [_profile_card_summary(card) for card in cards],
        "count": len(cards),
        "limit": max(0, limit),
        "read_only": True,
    }


def _agent_memory_summary(memory: AgentMemory) -> dict[str, Any]:
    source_refs = _source_ref_summaries(memory.source_refs)
    status = "forgotten" if memory.decay_policy == "forgotten" or memory.confidence <= 0 else "active"
    return {
        "agent_memory_id": memory.agent_memory_id,
        "owner_user_id": memory.owner_user_id,
        "layer": memory.layer.value if hasattr(memory.layer, "value") else str(memory.layer),
        "text": memory.text,
        "confidence": memory.confidence,
        "source_refs": source_refs,
        "source_ref_status": "present" if source_refs else "missing",
        "last_verified_at": memory.last_verified_at,
        "status": status,
        "promotion_status": "forgotten" if status == "forgotten" else ("updated" if memory.last_verified_at else "promoted"),
        "decay_policy": memory.decay_policy,
        "created_by_user_id": memory.created_by_user_id,
    }


def _profile_card_summary(card: UserProfileCard) -> dict[str, Any]:
    source_refs = _source_ref_summaries(card.source_refs)
    return {
        "profile_card_id": card.profile_card_id,
        "owner_user_id": card.owner_user_id,
        "profile": card.profile,
        "confidence": card.confidence,
        "source_refs": source_refs,
        "source_ref_status": "present" if source_refs else "missing",
        "last_verified_at": card.last_verified_at,
        "status": "active",
        "promotion_status": "updated" if card.last_verified_at else "promoted",
    }


def _source_ref_summaries(source_refs: Sequence[SourceRef]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in asdict(ref).items() if value}
        for ref in source_refs
    ]


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
    pska_config = getattr(args, "pska_config", PSKAConfig.load(getattr(args, "config", None)))
    config = pska_config.embedding_runtime_config()
    config = EmbeddingConfig(
        provider=getattr(args, "embedding_provider", None) or config.provider,
        model=getattr(args, "embedding_model", None) or config.model,
        dimensions=getattr(args, "embedding_dimensions", None) or config.dimensions,
        batch_size=getattr(args, "batch_size", None) or config.batch_size,
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


def _mvp_status_payload(database_url: str, config: PSKAConfig | None = None) -> dict[str, Any]:
    api = _build_api(database_url, config)
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
    agentic_service = checks.get("agentic_service") or {}
    return {
        "ok": bool(payload.get("ok")),
        "database_url": payload.get("database_url"),
        "database_ok": bool((checks.get("database") or {}).get("ok")),
        "schema_ok": bool((checks.get("schema") or {}).get("ok")),
        "mcp_ok": bool((checks.get("mcp") or {}).get("ok")),
        "agentic_service_ok": bool(agentic_service.get("ok")),
        "agentic_service_provider": agentic_service.get("provider"),
        "agentic_service_adapter": agentic_service.get("adapter"),
        "agentic_service_pska_tools_loaded": bool(agentic_service.get("pska_tools_loaded")),
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


def _daily_status_payload(
    database_url: str,
    *,
    owner_user_id: str = "user_primary",
    limit: int = 5,
    pska_config: PSKAConfig | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    limit = max(0, limit)
    status = _mvp_status_payload(database_url, pska_config)
    summary = _mvp_status_summary(status)
    jobs = status.get("jobs") or {}
    metrics = status.get("metrics") or {}
    index = metrics.get("index") or {}
    checks = ((status.get("ready") or {}).get("checks") or {})

    try:
        review_items = _build_api(database_url, pska_config).store.list_review_items()
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
        config_path=config_path,
    )

    return {
        "ok": bool(summary.get("database_ok")) and bool(summary.get("schema_ok")) and bool(summary.get("mcp_ok")),
        "database_url": database_url,
        "owner_user_id": owner_user_id,
        "requires_agentic_service_online": False,
        "service_readiness": {
            "database_ok": bool(summary.get("database_ok")),
            "schema_ok": bool(summary.get("schema_ok")),
            "mcp_ok": bool(summary.get("mcp_ok")),
            "jobs_ok": bool((checks.get("jobs") or {}).get("ok")),
            "metrics_ok": bool((checks.get("metrics") or {}).get("ok")),
            "agentic_service_ok": bool(summary.get("agentic_service_ok")),
            "agentic_service_provider": summary.get("agentic_service_provider"),
            "agentic_service_adapter": summary.get("agentic_service_adapter"),
            "agentic_service_optional_for_daily_status": True,
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


def _daily_briefing_payload(
    database_url: str,
    *,
    owner_user_id: str = "user_primary",
    limit: int = 5,
    narrative: bool = False,
    narrative_timeout_seconds: float | None = None,
    fastreact_client=None,
    pska_config: PSKAConfig | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    limit = max(0, limit)
    status = _mvp_status_payload(database_url, pska_config)
    summary = _mvp_status_summary(status)
    daily = _daily_status_payload(
        database_url,
        owner_user_id=owner_user_id,
        limit=limit,
        pska_config=pska_config,
        config_path=config_path,
    )
    metrics = status.get("metrics") or {}
    connectors = (metrics.get("connectors") or {})
    store = _build_api(database_url, pska_config).store
    recent_sources = _recent_source_items(store.list_source_items(), owner_user_id=owner_user_id, limit=limit)
    pending_review_count = int((daily.get("pending_reviews") or {}).get("total_matching") or 0)
    failed_job_count = int((daily.get("failed_jobs") or {}).get("count") or 0)
    digest_backlog_count = int((daily.get("digest_backlog") or {}).get("jobs") or 0)
    next_actions = _daily_briefing_next_actions(
        summary=summary,
        pending_review_count=pending_review_count,
        failed_job_count=failed_job_count,
        digest_backlog_count=digest_backlog_count,
        config_path=config_path,
    )
    recommended_commands = list(dict.fromkeys([*daily.get("recommended_commands", []), *next_actions]))
    payload = {
        "ok": bool(daily.get("ok")),
        "database_url": database_url,
        "owner_user_id": owner_user_id,
        "briefing_type": "deterministic_daily_v0",
        "requires_llm": bool(narrative),
        "requires_agentic_service_online": False,
        "requires_fastreact_online": bool(narrative),
        "service_readiness": daily.get("service_readiness") or {},
        "source_summary": {
            "counts": daily.get("source_counts") or {},
            "recent_sources": recent_sources,
        },
        "connector_state": {
            "source_channels": sorted((connectors.get("source_channels") or {}).keys()),
            "state_count": int(connectors.get("state_count") or 0),
            "enabled_state_count": int(connectors.get("enabled_state_count") or 0),
            "state_sync_status": connectors.get("state_sync_status") or {},
        },
        "digest_backlog": daily.get("digest_backlog") or {},
        "pending_reviews": daily.get("pending_reviews") or {},
        "failed_jobs": daily.get("failed_jobs") or {},
        "deterministic_next_actions": next_actions,
        "recommended_commands": recommended_commands,
        "notes": [
            "This briefing is deterministic JSON assembled from PSKA DB state.",
            "FastReAct can be offline; narrative generation belongs to HW-005.",
        ],
    }
    if narrative:
        payload["narrative"] = _daily_briefing_narrative(
            store,
            payload,
            owner_user_id=owner_user_id,
            timeout_seconds=narrative_timeout_seconds,
            fastreact_client=fastreact_client,
            pska_config=pska_config,
        )
    return payload


def _ops_briefing_payload(
    database_url: str,
    *,
    owner_user_id: str = "user_primary",
    limit: int = 5,
    connector_stale_seconds: int = 86_400,
    pska_config: PSKAConfig | None = None,
) -> dict[str, Any]:
    limit = max(0, limit)
    connector_stale_seconds = max(0, connector_stale_seconds)
    api = _build_api(database_url, pska_config)
    try:
        ready = api.ready()
    except Exception as exc:  # noqa: BLE001 - ops should report service diagnostics, not crash.
        ready = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "checks": {}}
    try:
        metrics = api.metrics()
    except Exception as exc:  # noqa: BLE001
        metrics = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "index": {}, "connectors": {}}
    try:
        jobs = api.job_stats()["stats"]
    except Exception as exc:  # noqa: BLE001
        jobs = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "by_status": {}, "digest_backlog": {}, "stale_running": []}

    store = getattr(api, "store", None)
    failed_jobs = _ops_list_jobs(store, status="failed", limit=max(limit, 20))
    running_jobs = _ops_list_jobs(store, status="running", limit=max(limit, 20))
    connector_states = _ops_connector_states(store, owner_user_id=owner_user_id)
    ready_checks = (ready.get("checks") or {}) if isinstance(ready, dict) else {}
    connectors = (metrics.get("connectors") or {}) if isinstance(metrics, dict) else {}
    digest_backlog = jobs.get("digest_backlog") or {}
    recent_failed = list(jobs.get("recent_failed") or [])
    if not recent_failed:
        recent_failed = [_ops_job_summary(job) for job in failed_jobs[:limit]]
    failed_digest_jobs = [
        item
        for item in recent_failed
        if str(item.get("job_type") or "") == "digest_via_fastreact"
    ][:limit]
    stale_running = list(jobs.get("stale_running") or [])
    if not stale_running:
        stale_running = [_ops_job_summary(job) for job in running_jobs if _ops_job_is_stale(job)][:limit]
    connector_findings = _ops_connector_findings(connector_states, stale_seconds=connector_stale_seconds)

    issues = [
        _ops_service_issue(ready, ready_checks),
        _ops_agentic_service_issue(ready_checks.get("agentic_service") or {}),
        _ops_stale_job_issue(stale_running),
        _ops_failed_digest_issue(failed_digest_jobs),
        _ops_connector_stale_issue(connector_findings),
        _ops_empty_backlog_issue(digest_backlog),
    ]
    recommended_commands = []
    for issue in issues:
        recommended_commands.extend(issue["recovery_commands"])

    by_status = jobs.get("by_status") or {}
    return {
        "ok": not any(issue["severity"] == "critical" for issue in issues),
        "database_url": database_url,
        "owner_user_id": owner_user_id,
        "briefing_type": "deterministic_ops_v0",
        "requires_llm": False,
        "requires_agentic_service_online": False,
        "service_readiness": {
            "ok": bool(ready.get("ok")) if isinstance(ready, dict) else False,
            "database_ok": bool((ready_checks.get("database") or {}).get("ok")),
            "schema_ok": bool((ready_checks.get("schema") or {}).get("ok")),
            "mcp_ok": bool((ready_checks.get("mcp") or {}).get("ok")),
            "jobs_ok": bool((ready_checks.get("jobs") or {}).get("ok")),
            "metrics_ok": bool((ready_checks.get("metrics") or {}).get("ok")),
            "agentic_service_ok": bool((ready_checks.get("agentic_service") or {}).get("ok")),
            "agentic_service_provider": (ready_checks.get("agentic_service") or {}).get("provider"),
            "agentic_service_adapter": (ready_checks.get("agentic_service") or {}).get("adapter"),
            "error": ready.get("error") if isinstance(ready, dict) else None,
        },
        "worker_health": {
            "by_status": by_status,
            "running": int(by_status.get("running") or len(running_jobs)),
            "failed": int(by_status.get("failed") or len(failed_jobs)),
            "active_worker_ids": sorted(set(jobs.get("active_worker_ids") or [getattr(job, "worker_id", None) for job in running_jobs if getattr(job, "worker_id", None)])),
            "stale_running_count": len(stale_running),
            "stale_running": stale_running[:limit],
        },
        "digest_quality": {
            "backlog": digest_backlog,
            "failed_digest_count": len(failed_digest_jobs),
            "failed_digest_jobs": failed_digest_jobs,
        },
        "connector_state": {
            "source_channels": sorted((connectors.get("source_channels") or {}).keys()),
            "state_count": int(connectors.get("state_count") or len(connector_states)),
            "enabled_state_count": int(connectors.get("enabled_state_count") or sum(1 for state in connector_states if getattr(state, "enabled", False))),
            "state_sync_status": connectors.get("state_sync_status") or {},
            "stale_seconds": connector_stale_seconds,
            "findings": connector_findings[:limit],
        },
        "issues": issues,
        "recommended_recovery_commands": list(dict.fromkeys(recommended_commands)),
        "notes": [
            "This briefing is deterministic and does not call an LLM.",
            "Agentic service can be offline; digest jobs remain visible as backlog until the configured adapter handles them.",
        ],
    }


def _ops_service_issue(ready: dict[str, Any], checks: dict[str, Any]) -> dict[str, Any]:
    failed = [
        name
        for name in ["database", "schema", "mcp", "jobs", "metrics"]
        if (checks.get(name) or {}).get("ok") is not True
    ]
    if ready.get("ok") is True and not failed:
        status = "ok"
        severity = "info"
        summary = "PSKA service checks are healthy."
    else:
        status = "service_down"
        severity = "critical"
        summary = "PSKA service readiness is failing."
    return {
        "id": "service_readiness",
        "status": status,
        "severity": severity,
        "summary": summary,
        "diagnostics": {"failed_checks": failed, "error": ready.get("error")},
        "recovery_commands": [] if status == "ok" else ["./scripts/pska db-init", "./scripts/pska service-check"],
    }


def _ops_agentic_service_issue(agentic_service: dict[str, Any]) -> dict[str, Any]:
    ok = agentic_service.get("ok") is True
    return {
        "id": "agentic_service",
        "status": "ok" if ok else "agentic_service_down",
        "severity": "info" if ok else "warning",
        "summary": "Agentic service is reachable." if ok else "Agentic service is offline or not authorized.",
        "diagnostics": {
            "provider": agentic_service.get("provider"),
            "adapter": agentic_service.get("adapter"),
            "error": agentic_service.get("error"),
            "pska_tools_loaded": agentic_service.get("pska_tools_loaded"),
        },
        "recovery_commands": [] if ok else ["./scripts/pska fastreact-digest-worker-command", "./scripts/pska service-check"],
    }


def _ops_stale_job_issue(stale_running: list[dict[str, Any]]) -> dict[str, Any]:
    has_stale = bool(stale_running)
    return {
        "id": "stale_jobs",
        "status": "stale_job" if has_stale else "ok",
        "severity": "warning" if has_stale else "info",
        "summary": f"{len(stale_running)} stale running job(s) need recovery." if has_stale else "No stale running jobs detected.",
        "diagnostics": {"stale_running": stale_running},
        "recovery_commands": ["./scripts/pska job-recover --max-age-seconds 900", "./scripts/pska jobs list --status running"] if has_stale else [],
    }


def _ops_failed_digest_issue(failed_digest_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    has_failed = bool(failed_digest_jobs)
    return {
        "id": "failed_digest",
        "status": "failed_digest" if has_failed else "ok",
        "severity": "warning" if has_failed else "info",
        "summary": f"{len(failed_digest_jobs)} failed digest job(s) need inspection." if has_failed else "No failed digest jobs in the recent sample.",
        "diagnostics": {"failed_digest_jobs": failed_digest_jobs},
        "recovery_commands": ["./scripts/pska jobs list --status failed --job-type digest_via_fastreact", "./scripts/pska fastreact-digest-worker-command"] if has_failed else [],
    }


def _ops_connector_stale_issue(findings: list[dict[str, Any]]) -> dict[str, Any]:
    stale = [finding for finding in findings if finding["status"] != "ok"]
    return {
        "id": "connector_freshness",
        "status": "connector_stale" if stale else "ok",
        "severity": "warning" if stale else "info",
        "summary": f"{len(stale)} connector state(s) are stale or failing." if stale else "Connector states look fresh.",
        "diagnostics": {"stale_or_failing": stale},
        "recovery_commands": ["./scripts/pska files-sync", "./scripts/pska connector-state list"] if stale else [],
    }


def _ops_empty_backlog_issue(digest_backlog: dict[str, Any]) -> dict[str, Any]:
    jobs = int(digest_backlog.get("jobs") or 0)
    return {
        "id": "digest_backlog",
        "status": "empty_backlog" if jobs == 0 else "backlog_present",
        "severity": "info",
        "summary": "Digest backlog is empty." if jobs == 0 else f"Digest backlog has {jobs} job(s).",
        "diagnostics": {"digest_backlog": digest_backlog},
        "recovery_commands": ["./scripts/pska digest-schedule --owner-user-id user_primary"] if jobs == 0 else ["./scripts/pska fastreact-digest-worker-command"],
    }


def _ops_connector_states(store, *, owner_user_id: str) -> list[Any]:
    if store is None or not hasattr(store, "list_connector_states"):
        return []
    try:
        return list(store.list_connector_states(owner_user_id=owner_user_id))
    except TypeError:
        return list(store.list_connector_states())
    except Exception:
        return []


def _ops_connector_findings(states: Sequence[Any], *, stale_seconds: int) -> list[dict[str, Any]]:
    now = utc_now()
    findings = []
    for state in states:
        last_success_at = getattr(state, "last_success_at", None)
        last_error_at = getattr(state, "last_error_at", None)
        sync_status = str(getattr(state, "sync_status", "") or "unknown")
        age_seconds = int((now - last_success_at).total_seconds()) if last_success_at else None
        stale = bool(getattr(state, "enabled", False)) and (last_success_at is None or age_seconds > stale_seconds or sync_status in {"failed", "error"})
        findings.append(
            {
                "connector_state_id": getattr(state, "connector_state_id", None),
                "connector_id": getattr(state, "connector_id", None),
                "status": "connector_stale" if stale else "ok",
                "sync_status": sync_status,
                "last_success_at": last_success_at,
                "last_error_at": last_error_at,
                "last_error": getattr(state, "last_error", None),
                "age_seconds": age_seconds,
            }
        )
    return findings


def _ops_list_jobs(store, *, status: str, limit: int) -> list[Any]:
    if store is None or not hasattr(store, "list_jobs"):
        return []
    try:
        return list(store.list_jobs(status=status, limit=limit))
    except TypeError:
        return [job for job in store.list_jobs(limit=limit) if getattr(job, "status", None) == status]
    except Exception:
        return []


def _ops_job_is_stale(job) -> bool:
    leased_until = getattr(job, "leased_until", None)
    return bool(getattr(job, "status", None) == "running" and leased_until and leased_until < utc_now())


def _ops_job_summary(job) -> dict[str, Any]:
    return {
        "job_id": getattr(job, "job_id", None),
        "job_type": getattr(job, "job_type", None),
        "status": getattr(job, "status", None),
        "worker_id": getattr(job, "worker_id", None),
        "leased_until": getattr(job, "leased_until", None),
        "error": getattr(job, "error", None),
    }


def _ops_briefing_text(payload: dict[str, Any]) -> str:
    lines = [
        f"PSKA Ops Briefing ({payload.get('database_url')})",
        f"ok={str(payload.get('ok')).lower()} type={payload.get('briefing_type')}",
        "",
        "Issues:",
    ]
    for issue in payload.get("issues") or []:
        lines.append(f"- {issue['id']}: {issue['status']} [{issue['severity']}] {issue['summary']}")
        for command in issue.get("recovery_commands") or []:
            lines.append(f"  recovery: {command}")
    lines.append("")
    lines.append("Recommended recovery commands:")
    for command in payload.get("recommended_recovery_commands") or []:
        lines.append(f"- {command}")
    if not payload.get("recommended_recovery_commands"):
        lines.append("- none")
    return "\n".join(lines)


def _recent_source_items(items: Sequence[SourceItem], *, owner_user_id: str, limit: int) -> list[dict[str, Any]]:
    visible = [item for item in items if item.owner_user_id == owner_user_id]
    recent = sorted(visible, key=lambda item: (item.created_at, item.source_item_id), reverse=True)[:limit]
    return [
        {
            "source_item_id": item.source_item_id,
            "source_channel": item.source_channel,
            "record_type": item.record_type,
            "title": item.title,
            "url": item.url,
            "created_at": item.created_at,
        }
        for item in recent
    ]


def _daily_briefing_next_actions(
    *,
    summary: dict[str, Any],
    pending_review_count: int,
    failed_job_count: int,
    digest_backlog_count: int,
    config_path: str | None = None,
) -> list[str]:
    actions: list[str] = []
    pska = _pska_command(config_path)
    counts = summary.get("counts") or {}
    connectors = summary.get("connectors") or {}
    if not summary.get("database_ok") or not summary.get("schema_ok") or not summary.get("mcp_ok"):
        actions.append(f"{pska} db-init")
        actions.append(f"{pska} service-check")
    if int(counts.get("source_items") or 0) == 0:
        actions.append(f"{pska} mvp-bootstrap")
    if not connectors.get("state_count"):
        actions.append(f"{pska} files-sync")
    if pending_review_count:
        actions.append(f"{pska} review-list --status pending --owner-user-id user_primary --summary")
    if failed_job_count:
        actions.append(f"{pska} jobs list --status failed")
    if digest_backlog_count:
        actions.append(f"{pska} fastreact-digest-worker-command")
    elif int(counts.get("source_items") or 0) > 0:
        actions.append(f"{pska} digest-schedule --owner-user-id user_primary")
    if int(counts.get("source_items") or 0) > 0:
        actions.append(f"{pska} memory-list --owner-user-id user_primary --limit 5")
        actions.append(f"{pska} profile-list --owner-user-id user_primary --limit 5")
    if not actions:
        actions.append(f"{pska} daily-briefing")
    return list(dict.fromkeys(actions))


def _daily_briefing_narrative(
    store,
    briefing: dict[str, Any],
    *,
    owner_user_id: str,
    timeout_seconds: float | None = None,
    fastreact_client=None,
    pska_config: PSKAConfig | None = None,
) -> dict[str, Any]:
    source_refs = _briefing_source_refs(briefing)
    trace_summary: dict[str, Any] = {}
    try:
        client = fastreact_client or _fastreact_client(timeout_seconds=timeout_seconds, pska_config=pska_config)
        response = client.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": "Rewrite the user-provided PSKA facts into a concise Chinese daily briefing. Do not call tools.",
                },
                {"role": "user", "content": _narrative_briefing_text(briefing)},
            ],
            user_id=owner_user_id,
            purpose="pska_narrative_briefing",
            stream=False,
            scope={"source_refs": source_refs},
            temperature=0.3,
            top_p=0.9,
            max_tokens=4096,
        )
        answer = _fastreact_response_text(response)
        if not answer:
            raise FastreactError("FastReAct daily briefing returned no narrative content")
        cited_refs = _fastreact_response_source_refs(response) or source_refs
        trace_summary = _fastreact_trace_summary(response)
        saved = _save_narrative_briefing_source(
            store,
            owner_user_id=owner_user_id,
            answer=answer,
            source_refs=cited_refs,
            trace_summary=trace_summary,
            response=response,
        )
        return {
            "attempted": True,
            "ok": True,
            "fallback": False,
            "answer": answer,
            "source_refs": cited_refs,
            "trace_summary": trace_summary,
            "saved_source_item_id": saved.source_item_id,
        }
    except Exception as exc:  # noqa: BLE001 - narrative must never break deterministic briefing.
        return {
            "attempted": True,
            "ok": False,
            "fallback": True,
            "error": f"{type(exc).__name__}: {exc}",
            "source_refs": source_refs,
            "trace_summary": trace_summary,
            "timeout_seconds": timeout_seconds,
        }


def _fastreact_client(*, timeout_seconds: float | None = None, pska_config: PSKAConfig | None = None) -> HttpFastreactClient:
    config = pska_config.fastreact_runtime_config() if pska_config else FastreactConfig.from_env()
    if timeout_seconds is not None:
        config = FastreactConfig(
            url=config.url,
            service_token=config.service_token,
            timeout_seconds=max(1.0, float(timeout_seconds)),
        )
    return HttpFastreactClient(config)


def _narrative_briefing_context(briefing: dict[str, Any]) -> dict[str, Any]:
    failed_jobs = briefing.get("failed_jobs") or {}
    recent_failed = []
    for job in failed_jobs.get("recent", []) or []:
        if not isinstance(job, dict):
            continue
        recent_failed.append(
            {
                "job_id": job.get("job_id"),
                "job_type": job.get("job_type"),
                "error": str(job.get("error") or "")[:240],
            }
        )
    return {
        "owner_user_id": briefing.get("owner_user_id"),
        "service_readiness": briefing.get("service_readiness"),
        "source_counts": (briefing.get("source_summary") or {}).get("counts"),
        "recent_sources": (briefing.get("source_summary") or {}).get("recent_sources"),
        "connector_state": briefing.get("connector_state"),
        "digest_backlog": briefing.get("digest_backlog"),
        "pending_review_count": (briefing.get("pending_reviews") or {}).get("total_matching"),
        "failed_jobs": {
            "count": failed_jobs.get("count"),
            "recent": recent_failed,
        },
        "deterministic_next_actions": briefing.get("deterministic_next_actions"),
    }


def _narrative_briefing_text(briefing: dict[str, Any]) -> str:
    context = _narrative_briefing_context(briefing)
    recent_titles = [
        str(source.get("title") or source.get("source_item_id"))
        for source in context.get("recent_sources", [])
        if isinstance(source, dict)
    ]
    action_labels = [
        str(action).replace("./scripts/pska ", "")
        for action in context.get("deterministic_next_actions", [])
    ]
    failed_jobs = context.get("failed_jobs") or {}
    return "\n".join(
        [
            "请把以下确定性 PSKA 状态改写成 3 句以内的中文日常简报；只基于这些事实，不要请求工具或额外数据。",
            f"资料规模：source_items={((context.get('source_counts') or {}).get('source_items') or 0)}, chunks={((context.get('source_counts') or {}).get('chunks') or 0)}。",
            f"最近资料：{', '.join(recent_titles) if recent_titles else '无'}。",
            f"connector：channels={', '.join((context.get('connector_state') or {}).get('source_channels') or [])}, state_count={(context.get('connector_state') or {}).get('state_count') or 0}。",
            f"digest backlog：jobs={(context.get('digest_backlog') or {}).get('jobs') or 0}, source_items={(context.get('digest_backlog') or {}).get('source_items') or 0}。",
            f"pending reviews：{context.get('pending_review_count') or 0}；failed jobs：{failed_jobs.get('count') or 0}。",
            f"建议动作：{'; '.join(action_labels) if action_labels else '无需动作'}。",
        ]
    )


def _briefing_source_refs(briefing: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for source in ((briefing.get("source_summary") or {}).get("recent_sources") or []):
        if isinstance(source, dict) and source.get("source_item_id"):
            refs.append({"source_item_id": str(source["source_item_id"])})
    for review in ((briefing.get("pending_reviews") or {}).get("review_items") or []):
        if isinstance(review, dict):
            refs.extend(ref for ref in review.get("source_refs", []) if isinstance(ref, dict))
    return _dedupe_source_ref_dicts(refs)


def _fastreact_response_text(response: dict[str, Any]) -> str:
    for key in ["content", "final_content", "answer", "text", "message"]:
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    trace = response.get("trace")
    if isinstance(trace, dict):
        for key in ["final_content", "content", "answer"]:
            value = trace.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"].strip()
            if isinstance(first.get("text"), str):
                return first["text"].strip()
    return ""


def _fastreact_response_source_refs(response: dict[str, Any]) -> list[dict[str, Any]]:
    refs = response.get("source_refs") or response.get("citations")
    if not isinstance(refs, list):
        return []
    normalized = []
    allowed = set(SourceRef.__dataclass_fields__)
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        source_ref = {key: value for key, value in ref.items() if key in allowed and value}
        if source_ref:
            normalized.append(source_ref)
    return _dedupe_source_ref_dicts(normalized)


def _fastreact_trace_summary(response: dict[str, Any]) -> dict[str, Any]:
    return {
        key: response[key]
        for key in ["run_id", "session_id", "model", "usage", "tool_calls", "event_count"]
        if key in response
    }


def _save_narrative_briefing_source(
    store,
    *,
    owner_user_id: str,
    answer: str,
    source_refs: list[dict[str, Any]],
    trace_summary: dict[str, Any],
    response: dict[str, Any],
) -> SourceItem:
    return capture_agent_conversation(
        store,
        owner_user_id=owner_user_id,
        represented_user_id=owner_user_id,
        purpose="daily_briefing",
        prompt="Generate narrative daily briefing from deterministic PSKA context.",
        answer=answer,
        source_refs=source_refs,
        trace_summary=trace_summary,
        title="FastReAct daily briefing",
        source_channel="pska_briefing",
        conversation_id=str(response.get("run_id") or f"daily_narrative_{int(time.time())}"),
        tool_calls=list(response.get("tool_calls") or []),
    )


def _dedupe_source_ref_dicts(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for ref in refs:
        normalized = {key: value for key, value in ref.items() if value}
        key = tuple(sorted(normalized.items()))
        if normalized and key not in seen:
            seen.add(key)
            deduped.append(normalized)
    return deduped


def _daily_status_recommended_commands(
    *,
    summary: dict[str, Any],
    pending_review_count: int,
    failed_job_count: int,
    config_path: str | None = None,
) -> list[str]:
    pska = _pska_command(config_path)
    commands = [f"{pska} daily-status", f"{pska} mvp-status --summary"]
    counts = summary.get("counts") or {}
    digest_backlog = ((summary.get("jobs") or {}).get("digest_backlog") or {}).get("jobs") or 0
    if int(counts.get("source_items") or 0) == 0:
        commands.append(f"{pska} mvp-bootstrap")
    if int(counts.get("source_items") or 0) > 0 and int(counts.get("entities") or 0) == 0:
        commands.append(f"{pska} extract-all --owner-user-id user_primary")
    if digest_backlog:
        commands.append(f"{pska} fastreact-digest-worker-command")
    elif int(counts.get("source_items") or 0) > 0:
        commands.append(f"{pska} digest-schedule --owner-user-id user_primary")
    if pending_review_count:
        commands.append(f"{pska} review-list --status pending --owner-user-id user_primary --summary")
        commands.append(f"{pska} review-approve <review_item_id> --apply")
    if failed_job_count:
        commands.append(f"{pska} jobs list --status failed")
        commands.append(f"{pska} job-status --job-id <job_id>")
    return commands


def _pska_command(config_path: str | None = None) -> str:
    config = str(config_path or ".pska/config.json")
    return f"./scripts/pska --config {shlex_quote(config)}"


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
    if digest_backlog and checks.get("agentic_service", {}).get("ok") is True:
        actions.append("Run the configured agentic-service adapter worker to consume queued digest jobs.")
    if checks.get("agentic_service", {}).get("ok") is False:
        actions.append("Start the configured agentic service when you want agentic digest or agentic QA.")
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
