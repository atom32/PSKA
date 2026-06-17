from __future__ import annotations

from datetime import datetime, timezone
import json

import pska_core.cli as cli_module
from pska_core.config import PSKAConfig
from pska_core.cli import (
    build_parser,
    digest_scheduler,
    _fastreact_digest_worker_command_payload,
    _daily_status_payload,
    _mvp_next_actions,
    _mvp_status_payload,
    _mvp_status_summary,
    _review_items_payload,
)
from pska_core.enums import ReviewType
from pska_core.models import ReviewItem


def test_cli_accepts_db_check() -> None:
    args = build_parser().parse_args(["db-check"])

    assert args.command == "db-check"
    assert args.database_url is None


def test_cli_accepts_database_url_override() -> None:
    args = build_parser().parse_args(["--database-url", "postgresql:///example", "db-init"])

    assert args.command == "db-init"
    assert args.database_url == "postgresql:///example"


def test_cli_accepts_config_path() -> None:
    args = build_parser().parse_args(["--config", "config.pska.json", "db-check"])

    assert args.command == "db-check"
    assert str(args.config) == "config.pska.json"


def test_cli_accepts_import_twitter_zips() -> None:
    args = build_parser().parse_args([
        "--database-url",
        "postgresql:///example",
        "import-twitter-zips",
        "--input",
        "zips",
        "--visible-team-ids",
        "team_a,team_b",
    ])

    assert args.command == "import-twitter-zips"
    assert str(args.input) == "zips"
    assert args.visible_team_ids == "team_a,team_b"


def test_cli_accepts_search_and_smoke() -> None:
    search = build_parser().parse_args(["search", "--query", "hello", "--top-k", "3"])
    smoke = build_parser().parse_args(["smoke-twitter-import"])
    agentic = build_parser().parse_args(["agentic-search", "--query", "hello"])
    extract = build_parser().parse_args(["extract-all", "--owner-user-id", "user_primary"])
    serve = build_parser().parse_args(["serve", "--port", "8765"])
    local_daemon = build_parser().parse_args(["local-daemon", "--no-worker", "--digest-interval-seconds", "60"])
    mvp_bootstrap = build_parser().parse_args(["mvp-bootstrap", "--notes-root", "notes", "--dry-run", "--extract"])
    mvp_status = build_parser().parse_args(["mvp-status", "--summary"])
    daily_status = build_parser().parse_args(["daily-status", "--owner-user-id", "user_primary", "--limit", "3"])
    digest_worker_command = build_parser().parse_args(["fastreact-digest-worker-command", "--batch-limit", "3"])
    service_check = build_parser().parse_args([
        "service-check",
        "--url",
        "http://127.0.0.1:8765",
        "--timeout-seconds",
        "1",
        "--expected-database-url",
        "postgresql:///pska",
    ])
    embed = build_parser().parse_args(["embed-backfill", "--embedding-provider", "bge-m3", "--limit", "10"])
    mcp = build_parser().parse_args(["mcp-server"])
    connector = build_parser().parse_args(["connector-ingest-record", "record.json"])
    files_scan = build_parser().parse_args(["files-scan", "--root", "notes", "--ignore", "*.tmp"])
    files_sync = build_parser().parse_args(["files-sync", "--root", "notes", "--ignore", "*.tmp"])
    files_watch = build_parser().parse_args(["files-watch", "--root", "notes", "--initial-sync", "--max-events", "1"])

    assert search.command == "search"
    assert search.query == "hello"
    assert search.top_k == 3
    assert smoke.command == "smoke-twitter-import"
    assert agentic.command == "agentic-search"
    assert extract.command == "extract-all"
    assert serve.command == "serve"
    assert local_daemon.command == "local-daemon"
    assert local_daemon.no_worker is True
    assert local_daemon.digest_interval_seconds == 60
    assert mvp_bootstrap.command == "mvp-bootstrap"
    assert str(mvp_bootstrap.notes_root[0]) == "notes"
    assert mvp_bootstrap.dry_run is True
    assert mvp_bootstrap.extract is True
    assert mvp_status.command == "mvp-status"
    assert mvp_status.summary is True
    assert daily_status.command == "daily-status"
    assert daily_status.owner_user_id == "user_primary"
    assert daily_status.limit == 3
    assert digest_worker_command.command == "fastreact-digest-worker-command"
    assert digest_worker_command.batch_limit == 3
    assert service_check.command == "service-check"
    assert service_check.url == "http://127.0.0.1:8765"
    assert service_check.timeout_seconds == 1
    assert service_check.expected_database_url == "postgresql:///pska"
    assert embed.command == "embed-backfill"
    assert embed.embedding_provider == "bge-m3"
    assert embed.limit == 10
    assert mcp.command == "mcp-server"
    assert connector.command == "connector-ingest-record"
    assert str(connector.record) == "record.json"
    assert files_scan.command == "files-scan"
    assert str(files_scan.root) == "notes"
    assert files_scan.ignore == ["*.tmp"]
    assert files_sync.command == "files-sync"
    assert str(files_sync.root[0]) == "notes"
    assert files_sync.ignore == ["*.tmp"]
    assert files_watch.command == "files-watch"
    assert str(files_watch.root[0]) == "notes"
    assert files_watch.initial_sync is True
    assert files_watch.max_events == 1


def test_cli_accepts_connector_state_commands() -> None:
    upsert = build_parser().parse_args(
        [
            "connector-state",
            "upsert",
            "--connector-id",
            "files",
            "--owner-user-id",
            "user_primary",
            "--scan-cursor",
            "cursor_1",
        ]
    )
    show = build_parser().parse_args(["connector-state", "show", "conn_user_primary_files"])

    assert upsert.command == "connector-state"
    assert upsert.action == "upsert"
    assert upsert.connector_id == "files"
    assert show.connector_state_id == "conn_user_primary_files"


def test_cli_accepts_job_commands() -> None:
    submit = build_parser().parse_args(["job-submit", "extract_all", "--max-attempts", "2", "--run-now"])
    fastreact_extract = build_parser().parse_args(["job-submit", "extract_via_fastreact"])
    fastreact_digest = build_parser().parse_args(["job-submit", "digest_via_fastreact"])
    review_apply = build_parser().parse_args(["job-submit", "review_apply"])
    run = build_parser().parse_args(["job-run", "--limit", "5"])
    status = build_parser().parse_args(["job-status", "--status", "failed"])
    retry = build_parser().parse_args(["job-retry", "job_123"])

    assert submit.command == "job-submit"
    assert submit.job_type == "extract_all"
    assert fastreact_extract.job_type == "extract_via_fastreact"
    assert fastreact_digest.job_type == "digest_via_fastreact"
    assert review_apply.job_type == "review_apply"
    assert submit.max_attempts == 2
    assert submit.run_now is True
    assert run.limit == 5
    assert status.status == "failed"
    assert retry.job_id == "job_123"


def test_cli_accepts_review_commands() -> None:
    listing = build_parser().parse_args(
        ["review-list", "--status", "pending", "--owner-user-id", "user_primary", "--limit", "5", "--summary"]
    )
    assert listing.command == "review-list"
    assert listing.status == "pending"
    assert listing.owner_user_id == "user_primary"
    assert listing.limit == 5
    assert listing.summary is True

    approve = build_parser().parse_args(
        ["review-approve", "rev_123", "--actor-user-id", "user_primary", "--reason", "ok", "--apply"]
    )
    assert approve.command == "review-approve"
    assert approve.review_item_id == "rev_123"
    assert approve.actor_user_id == "user_primary"
    assert approve.reason == "ok"
    assert approve.apply is True

    reject = build_parser().parse_args(["review-reject", "rev_123", "--reason", "no"])
    assert reject.command == "review-reject"
    assert reject.reason == "no"

    apply = build_parser().parse_args(["review-apply", "rev_123"])
    assert apply.command == "review-apply"
    assert apply.review_item_id == "rev_123"


def test_review_items_payload_filters_and_summarizes() -> None:
    created_at = datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc)
    payload = _review_items_payload(
        [
            ReviewItem(
                review_item_id="rev_1",
                owner_user_id="user_primary",
                review_type=ReviewType.PROFILE_UPDATE,
                title="Profile update",
                proposal={
                    "profile_delta": {"topic": "PSKA"},
                    "source_refs": [{"source_item_id": "src_1", "chunk_id": "chk_1"}],
                    "confidence": 0.7,
                },
                created_at=created_at,
            ),
            ReviewItem(
                review_item_id="rev_2",
                owner_user_id="other",
                review_type=ReviewType.CONFLICT,
                title="Conflict",
                proposal={},
                status="approved",
            ),
        ],
        status="pending",
        owner_user_id="user_primary",
        limit=10,
        summary=True,
    )

    assert payload == {
        "review_items": [
            {
                "review_item_id": "rev_1",
                "owner_user_id": "user_primary",
                "review_type": "profile_update",
                "status": "pending",
                "title": "Profile update",
                "confidence": 0.7,
                "source_refs": [{"source_item_id": "src_1", "chunk_id": "chk_1"}],
                "source_ref_status": "present",
                "created_at": created_at,
                "recommended_actions": [
                    "./scripts/pska review-approve rev_1",
                    "./scripts/pska review-approve rev_1 --apply",
                    "./scripts/pska review-reject rev_1",
                ],
                "apply_supported": True,
                "can_apply_now": False,
            }
        ],
        "count": 1,
        "total_matching": 1,
        "limit": 10,
    }


def test_review_summary_distinguishes_candidate_types_and_missing_sources() -> None:
    review_types = [
        ReviewType.MEMORY_CANDIDATE,
        ReviewType.PROFILE_UPDATE,
        ReviewType.RELATIONSHIP_CANDIDATE,
        ReviewType.ACTION_CANDIDATE,
        ReviewType.CONFLICT,
        ReviewType.LOW_CONFIDENCE,
    ]
    items = [
        ReviewItem(
            review_item_id=f"rev_{review_type.value}",
            owner_user_id="user_primary",
            review_type=review_type,
            title=review_type.value,
            proposal={"confidence": 0.6, "source_refs": [{"source_item_id": f"src_{index}"}]} if index % 2 == 0 else {},
        )
        for index, review_type in enumerate(review_types)
    ]

    payload = _review_items_payload(items, status="pending", owner_user_id="user_primary", limit=10, summary=True)
    by_type = {item["review_type"]: item for item in payload["review_items"]}

    assert set(by_type) == {review_type.value for review_type in review_types}
    assert by_type["memory_candidate"]["source_ref_status"] == "present"
    assert by_type["profile_update"]["source_ref_status"] == "missing"
    assert by_type["profile_update"]["apply_supported"] is True
    assert by_type["memory_candidate"]["apply_supported"] is False
    assert by_type["relationship_candidate"]["confidence"] == 0.6
    assert "./scripts/pska review-reject rev_conflict" in by_type["conflict"]["recommended_actions"]


def test_cli_accepts_job_worker_commands() -> None:
    run = build_parser().parse_args(["job-run", "--until-empty", "--limit", "0", "--worker-id", "worker_a", "--lease-seconds", "45"])
    worker = build_parser().parse_args(
        [
            "job-worker",
            "--poll-interval",
            "0.1",
            "--max-jobs",
            "2",
            "--idle-limit",
            "1",
            "--recover-stale-seconds",
            "60",
            "--worker-id",
            "worker_b",
            "--lease-seconds",
            "90",
        ]
    )
    recover = build_parser().parse_args(["job-recover", "--max-age-seconds", "120"])

    assert run.command == "job-run"
    assert run.until_empty is True
    assert run.limit == 0
    assert run.worker_id == "worker_a"
    assert run.lease_seconds == 45
    assert worker.command == "job-worker"
    assert worker.poll_interval == 0.1
    assert worker.max_jobs == 2
    assert worker.idle_limit == 1
    assert worker.recover_stale_seconds == 60
    assert worker.worker_id == "worker_b"
    assert worker.lease_seconds == 90
    assert recover.command == "job-recover"
    assert recover.max_age_seconds == 120


def test_cli_accepts_digest_schedule() -> None:
    args = build_parser().parse_args(
        [
            "digest-schedule",
            "--owner-user-id",
            "user_primary",
            "--source-item-id",
            "src_1",
            "--limit",
            "3",
            "--batch-size",
            "2",
            "--priority",
            "5",
            "--max-attempts",
            "4",
            "--retry-backoff-seconds",
            "30",
            "--quota-window-seconds",
            "3600",
            "--max-jobs-per-window",
            "2",
            "--force",
            "--reason",
            "new import",
        ]
    )

    assert args.command == "digest-schedule"
    assert args.owner_user_id == "user_primary"
    assert args.source_item_ids == ["src_1"]
    assert args.limit == 3
    assert args.batch_size == 2
    assert args.priority == 5
    assert args.max_attempts == 4
    assert args.retry_backoff_seconds == 30
    assert args.quota_window_seconds == 3600
    assert args.max_jobs_per_window == 2
    assert args.force is True
    assert args.reason == "new import"


def test_cli_accepts_digest_scheduler() -> None:
    args = build_parser().parse_args(
        [
            "digest-scheduler",
            "--owner-user-id",
            "user_primary",
            "--interval-seconds",
            "0",
            "--max-cycles",
            "1",
            "--idle-limit",
            "1",
            "--limit",
            "5",
            "--batch-size",
            "2",
            "--priority",
            "3",
            "--max-backlog-jobs",
            "4",
            "--quota-window-seconds",
            "3600",
            "--max-jobs-per-window",
            "2",
            "--recover-stale-seconds",
            "60",
        ]
    )

    assert args.command == "digest-scheduler"
    assert args.owner_user_id == "user_primary"
    assert args.interval_seconds == 0
    assert args.max_cycles == 1
    assert args.idle_limit == 1
    assert args.limit == 5
    assert args.batch_size == 2
    assert args.priority == 3
    assert args.max_backlog_jobs == 4
    assert args.quota_window_seconds == 3600
    assert args.max_jobs_per_window == 2
    assert args.recover_stale_seconds == 60


def test_digest_scheduler_runs_one_foreground_cycle(monkeypatch, capsys) -> None:
    class FakeStore:
        def recover_stale_jobs(self, *, max_age_seconds):
            assert max_age_seconds == 60
            return []

    class FakeApi:
        def __init__(self, database_url):
            assert database_url == "postgresql:///example"
            self.store = FakeStore()

        def job_stats(self):
            return {"stats": {"digest_backlog": {"jobs": 0}}}

        def schedule_digest(self, payload):
            assert payload["owner_user_id"] == "user_primary"
            assert payload["limit"] == 5
            return {"scheduled_source_item_ids": ["src_1"], "job": {"job_id": "job_1"}}

    monkeypatch.setattr(cli_module, "PSKAApi", FakeApi)
    args = build_parser().parse_args(
        [
            "--database-url",
            "postgresql:///example",
            "digest-scheduler",
            "--interval-seconds",
            "0",
            "--max-cycles",
            "1",
            "--limit",
            "5",
            "--recover-stale-seconds",
            "60",
        ]
    )

    code = digest_scheduler(args)
    documents = _json_documents(capsys.readouterr().out)

    assert code == 0
    assert documents[0]["event"] == "digest_scheduler_cycle"
    assert documents[0]["scheduled"] is True
    assert documents[0]["cycle"] == 1
    assert documents[-1] == {"processed": 1, "idle_cycles": 0}


def test_mvp_next_actions_prioritize_missing_data_and_fastreact() -> None:
    actions = _mvp_next_actions(
        {
            "ready": {"ok": True, "checks": {"fastreact": {"ok": False}}},
            "metrics": {"index": {"source_items": 0, "entities": 0, "hyperedges": 0}},
            "jobs": {"digest_backlog": {"jobs": 1}},
            "pending_review_items": 0,
        },
        connectors={"state_count": 0},
    )

    assert any("mvp-bootstrap" in action for action in actions)
    assert any("files-scan" in action for action in actions)
    assert any("Fastreact" in action for action in actions)


def test_mvp_next_actions_surface_graph_and_digest_work() -> None:
    actions = _mvp_next_actions(
        {
            "ready": {"ok": True, "checks": {"fastreact": {"ok": True}}},
            "metrics": {"index": {"source_items": 3, "entities": 0, "hyperedges": 0}},
            "jobs": {"digest_backlog": {"jobs": 1}},
            "pending_review_items": 0,
        },
        connectors={"state_count": 1},
    )

    assert any("extract-all" in action for action in actions)
    assert any("digest worker" in action for action in actions)


def test_mvp_next_actions_ready_state() -> None:
    actions = _mvp_next_actions(
        {
            "ready": {"ok": True, "checks": {"fastreact": {"ok": True}}},
            "metrics": {"index": {"source_items": 3, "entities": 1, "hyperedges": 1}},
            "jobs": {"digest_backlog": {"jobs": 0}},
            "pending_review_items": 0,
        },
        connectors={"state_count": 1},
    )

    assert actions == ["System is ready for MVP use: run search, agentic-search, or keep local-daemon running."]


def test_mvp_status_summary_is_compact() -> None:
    summary = _mvp_status_summary(
        {
            "ok": True,
            "database_url": "postgresql:///pska",
            "ready": {
                "checks": {
                    "database": {"ok": True},
                    "schema": {"ok": True},
                    "mcp": {"ok": True},
                    "fastreact": {"ok": True, "pska_tools_loaded": True},
                }
            },
            "metrics": {
                "index": {"source_items": 3, "chunks": 3, "entities": 1, "hyperedges": 1, "review_items": 0, "jobs": 2},
                "connectors": {
                    "source_channels": {"twitter": {}, "files": {}},
                    "state_count": 1,
                    "enabled_state_count": 1,
                    "state_sync_status": {"succeeded": 1},
                },
            },
            "jobs": {"by_status": {"queued": 1}, "digest_backlog": {"jobs": 1}, "running_stale_count": 0},
            "pending_review_items": 0,
            "next_actions": ["ready"],
        }
    )

    assert summary["ok"] is True
    assert summary["database_url"] == "postgresql:///pska"
    assert summary["fastreact_pska_tools_loaded"] is True
    assert summary["counts"]["source_items"] == 3
    assert summary["connectors"]["source_channels"] == ["files", "twitter"]
    assert summary["next_actions"] == ["ready"]


def test_mvp_status_payload_reports_schema_drift(monkeypatch) -> None:
    class BrokenStore:
        def list_review_items(self):
            raise RuntimeError("review table missing")

    class BrokenApi:
        def __init__(self, database_url: str) -> None:
            self.store = BrokenStore()

        def ready(self):
            return {"ok": False, "checks": {"schema": {"ok": False, "missing": ["connector_states"]}}}

        def metrics(self):
            raise RuntimeError("connector_states missing")

        def job_stats(self):
            raise RuntimeError("jobs missing")

    monkeypatch.setattr(cli_module, "PSKAApi", BrokenApi)

    payload = _mvp_status_payload("postgresql:///pska")

    assert payload["ok"] is False
    assert payload["database_url"] == "postgresql:///pska"
    assert "connector_states missing" in payload["metrics"]["error"]
    assert "jobs missing" in payload["jobs"]["error"]
    assert any("db-init" in action for action in payload["next_actions"])


def test_daily_status_payload_is_deterministic_and_fastreact_optional(monkeypatch) -> None:
    class FakeStore:
        def list_review_items(self):
            return [
                ReviewItem(
                    review_item_id="rev_1",
                    owner_user_id="user_primary",
                    review_type=ReviewType.PROFILE_UPDATE,
                    title="Profile update",
                    proposal={"profile_delta": {"topic": "PSKA"}},
                ),
                ReviewItem(
                    review_item_id="rev_other",
                    owner_user_id="other",
                    review_type=ReviewType.CONFLICT,
                    title="Other conflict",
                    proposal={},
                ),
            ]

    class FakeApi:
        def __init__(self, database_url: str) -> None:
            assert database_url == "postgresql:///example"
            self.store = FakeStore()

        def ready(self):
            return {
                "ok": True,
                "checks": {
                    "database": {"ok": True},
                    "schema": {"ok": True},
                    "mcp": {"ok": True},
                    "jobs": {"ok": True},
                    "metrics": {"ok": True},
                    "fastreact": {"ok": False, "error": "offline"},
                },
            }

        def metrics(self):
            return {
                "index": {"source_items": 3, "chunks": 4, "entities": 1, "hyperedges": 1, "review_items": 2, "jobs": 3},
                "connectors": {"source_channels": {"twitter": {}}, "state_count": 1, "enabled_state_count": 1},
            }

        def job_stats(self):
            return {
                "stats": {
                    "by_status": {"queued": 1, "running": 0, "failed": 1, "succeeded": 1, "canceled": 0},
                    "digest_backlog": {"jobs": 1, "source_items": 1},
                    "running_stale_count": 0,
                    "recent_failed": [{"job_id": "job_1", "job_type": "extract_all", "error": "boom"}],
                }
            }

    monkeypatch.setattr(cli_module, "PSKAApi", FakeApi)

    payload = _daily_status_payload("postgresql:///example", owner_user_id="user_primary", limit=5)

    assert payload["ok"] is True
    assert payload["requires_fastreact_online"] is False
    assert payload["service_readiness"]["fastreact_ok"] is False
    assert payload["source_counts"] == {"source_items": 3, "chunks": 4}
    assert payload["digest_backlog"] == {"jobs": 1, "source_items": 1}
    assert payload["pending_reviews"]["total_matching"] == 1
    assert payload["failed_jobs"]["count"] == 1
    assert "./scripts/pska review-list --status pending --owner-user-id user_primary --summary" in payload["recommended_commands"]
    assert "./scripts/pska jobs list --status failed" in payload["recommended_commands"]


def test_fastreact_digest_worker_command_payload_uses_config_urls() -> None:
    args = build_parser().parse_args([
        "fastreact-digest-worker-command",
        "--fastreact-root",
        "/tmp/Fast React/fastreact-nano",
        "--batch-limit",
        "7",
        "--represented-user-id",
        "user_primary",
    ])
    config = PSKAConfig.from_dict(
        {
            "database": {"url": "postgresql:///pska"},
            "service": {"host": "127.0.0.1", "port": 8765},
            "fastreact": {"url": "http://127.0.0.1:8000"},
        }
    )

    payload = _fastreact_digest_worker_command_payload(args, config)

    assert payload["pska_database_url"] == "postgresql:///pska"
    assert payload["pska_url"] == "http://127.0.0.1:8765"
    assert payload["fastreact_url"] == "http://127.0.0.1:8000"
    assert "--batch-limit" in payload["command"]
    assert "'/tmp/Fast React/fastreact-nano'" in payload["shell"]


def test_files_sync_reports_missing_roots(capsys) -> None:
    args = build_parser().parse_args(["--database-url", "postgresql:///example", "files-sync"])

    code = cli_module.files_sync(args, PSKAConfig())

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert "files.roots" in payload["error"]


def test_files_watch_reports_missing_roots(capsys) -> None:
    args = build_parser().parse_args(["--database-url", "postgresql:///example", "files-watch"])

    code = cli_module.files_watch(args, PSKAConfig())

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert "files.roots" in payload["error"]


def _json_documents(output: str) -> list[dict]:
    decoder = json.JSONDecoder()
    documents = []
    offset = 0
    while output[offset:].strip():
        while offset < len(output) and output[offset].isspace():
            offset += 1
        document, end = decoder.raw_decode(output, offset)
        documents.append(document)
        offset = end
    return documents
