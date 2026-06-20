from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pska_core.cli as cli_module
from pska_core.config import PSKAConfig
from pska_core.cli import (
    build_parser,
    digest_scheduler,
    _daily_briefing_payload,
    _fastreact_digest_worker_command_payload,
    _daily_status_payload,
    _memory_list_payload,
    _mvp_next_actions,
    _mvp_status_payload,
    _mvp_status_summary,
    _ops_briefing_payload,
    _ops_briefing_text,
    _profile_list_payload,
    _review_batch_payload,
    _review_items_payload,
)
from pska_core.enums import MemoryLayer, ReviewType, Visibility
from pska_core.fastreact_client import FastreactError
from pska_core.models import AgentMemory, ConnectorState, Job, ReviewItem, SourceItem, SourceRef, UserProfileCard
from pska_core.store import InMemoryKnowledgeStore


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
    agentic = build_parser().parse_args(["agentic-search", "--query", "hello", "--capture"])
    extract = build_parser().parse_args(["extract-all", "--owner-user-id", "user_primary"])
    serve = build_parser().parse_args(["serve", "--port", "8765"])
    local_daemon = build_parser().parse_args(["local-daemon", "--no-worker", "--digest-interval-seconds", "60"])
    local_daemon_status = build_parser().parse_args(["local-daemon", "status", "--run-dir", "run", "--log-dir", "logs"])
    local_daemon_config = build_parser().parse_args(["local-daemon", "config-check"])
    local_daemon_supervisor = build_parser().parse_args(["local-daemon", "supervisor-config", "--supervisor", "launchd", "--dry-run"])
    job_worker = build_parser().parse_args(["job-worker", "--exclude-job-type", "digest_via_fastreact"])
    mvp_bootstrap = build_parser().parse_args(["mvp-bootstrap", "--notes-root", "notes", "--dry-run", "--extract"])
    mvp_status = build_parser().parse_args(["mvp-status", "--summary"])
    daily_status = build_parser().parse_args(["daily-status", "--owner-user-id", "user_primary", "--limit", "3"])
    daily_briefing = build_parser().parse_args(["daily-briefing", "--owner-user-id", "user_primary", "--limit", "3"])
    ops_briefing = build_parser().parse_args(["ops-briefing", "--owner-user-id", "user_primary", "--limit", "3", "--format", "text"])
    retrieval_eval = build_parser().parse_args(["retrieval-eval", "--fixture", "eval.json"])
    digest_worker_command = build_parser().parse_args(["fastreact-digest-worker-command", "--batch-limit", "3"])
    memory_list = build_parser().parse_args(["memory-list", "--owner-user-id", "user_primary", "--limit", "2"])
    profile_list = build_parser().parse_args(["profile-list", "--owner-user-id", "user_primary", "--limit", "2"])
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
    assert agentic.capture is True
    assert extract.command == "extract-all"
    assert serve.command == "serve"
    assert local_daemon.command == "local-daemon"
    assert local_daemon.action == "run"
    assert local_daemon.no_worker is True
    assert local_daemon.digest_interval_seconds == 60
    assert local_daemon_status.action == "status"
    assert str(local_daemon_status.run_dir) == "run"
    assert str(local_daemon_status.log_dir) == "logs"
    assert local_daemon_config.action == "config-check"
    assert local_daemon_supervisor.action == "supervisor-config"
    assert local_daemon_supervisor.supervisor == "launchd"
    assert local_daemon_supervisor.dry_run is True
    assert job_worker.command == "job-worker"
    assert job_worker.excluded_job_types == ["digest_via_fastreact"]
    assert mvp_bootstrap.command == "mvp-bootstrap"
    assert str(mvp_bootstrap.notes_root[0]) == "notes"
    assert mvp_bootstrap.dry_run is True
    assert mvp_bootstrap.extract is True
    assert mvp_status.command == "mvp-status"
    assert mvp_status.summary is True
    assert daily_status.command == "daily-status"
    assert daily_status.owner_user_id == "user_primary"
    assert daily_status.limit == 3
    assert daily_briefing.command == "daily-briefing"
    assert daily_briefing.owner_user_id == "user_primary"
    assert daily_briefing.limit == 3
    assert ops_briefing.command == "ops-briefing"
    assert ops_briefing.owner_user_id == "user_primary"
    assert ops_briefing.limit == 3
    assert ops_briefing.format == "text"
    assert retrieval_eval.command == "retrieval-eval"
    assert str(retrieval_eval.fixture) == "eval.json"
    narrative_briefing = build_parser().parse_args(["daily-briefing", "--narrative", "--narrative-timeout-seconds", "90"])
    assert narrative_briefing.narrative is True
    assert narrative_briefing.narrative_timeout_seconds == 90
    assert memory_list.command == "memory-list"
    assert memory_list.owner_user_id == "user_primary"
    assert memory_list.limit == 2
    assert profile_list.command == "profile-list"
    assert profile_list.owner_user_id == "user_primary"
    assert profile_list.limit == 2
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

    batch = build_parser().parse_args(
        [
            "review-batch",
            "apply",
            "--review-item-id",
            "rev_123",
            "--owner-user-id",
            "user_primary",
            "--review-type",
            "profile_update",
            "--status",
            "approved",
            "--execute",
        ]
    )
    assert batch.command == "review-batch"
    assert batch.action == "apply"
    assert batch.review_item_ids == ["rev_123"]
    assert batch.owner_user_id == "user_primary"
    assert batch.review_type == "profile_update"
    assert batch.status == "approved"
    assert batch.execute is True


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


def test_review_batch_dry_run_lists_processable_and_skipped_items() -> None:
    store = InMemoryKnowledgeStore()
    store.add_review_item(
        ReviewItem(
            review_item_id="rev_profile",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Profile update",
            proposal={
                "profile_delta": {"topic": "PSKA"},
                "source_refs": [{"source_item_id": "src_1"}],
                "confidence": 0.8,
            },
            status="approved",
        )
    )
    store.add_review_item(
        ReviewItem(
            review_item_id="rev_missing_refs",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Missing refs",
            proposal={"profile_delta": {"topic": "No refs"}},
            status="approved",
        )
    )
    store.add_review_item(
        ReviewItem(
            review_item_id="rev_share",
            owner_user_id="user_primary",
            review_type=ReviewType.SHARE_PROPOSAL,
            title="Share proposal",
            proposal={"source_refs": [{"source_item_id": "src_2"}]},
            status="approved",
        )
    )

    payload = _review_batch_payload(
        store,
        action="apply",
        review_item_ids=[],
        owner_user_id="user_primary",
        review_type=None,
        status="approved",
        limit=10,
        actor_user_id="user_primary",
        reason="batch dry run",
        dry_run=True,
    )

    assert payload["dry_run"] is True
    assert payload["summary"] == {"selected": 3, "to_process": 1, "skipped": 2, "affected": 0}
    assert [item["review_item_id"] for item in payload["to_process"]] == ["rev_profile"]
    skipped = {item["review_item_id"]: item["reason"] for item in payload["skipped"]}
    assert skipped == {
        "rev_missing_refs": "missing_source_refs",
        "rev_share": "batch_apply_requires_single_item_for_review_type",
    }
    assert store.list_audit_events() == []


def test_review_batch_apply_executes_safe_items_with_audit_and_source_refs() -> None:
    store = InMemoryKnowledgeStore()
    store.add_review_item(
        ReviewItem(
            review_item_id="rev_profile",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Profile update",
            proposal={
                "profile_delta": {"topic": "PSKA"},
                "source_refs": [{"source_item_id": "src_1", "chunk_id": "chk_1"}],
                "confidence": 0.8,
            },
            status="approved",
        )
    )

    payload = _review_batch_payload(
        store,
        action="apply",
        review_item_ids=["rev_profile"],
        owner_user_id=None,
        review_type=None,
        status=None,
        limit=10,
        actor_user_id="user_primary",
        reason="batch apply",
        dry_run=False,
    )

    assert payload["summary"] == {"selected": 1, "to_process": 1, "skipped": 0, "affected": 1}
    assert payload["affected_ids"] == ["rev_profile"]
    assert payload["results"][0]["review_item"]["status"] == "applied"
    assert payload["results"][0]["audit_events"][0]["decision"] == "applied"
    card = next(iter(store.profile_cards.values()))
    assert card.source_refs[0].source_item_id == "src_1"
    assert card.source_refs[0].chunk_id == "chk_1"


def test_review_batch_apply_requires_same_owner_and_type() -> None:
    store = InMemoryKnowledgeStore()
    store.add_review_item(
        ReviewItem(
            review_item_id="rev_primary",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Primary profile",
            proposal={"profile_delta": {"topic": "PSKA"}, "source_refs": [{"source_item_id": "src_1"}]},
            status="approved",
        )
    )
    store.add_review_item(
        ReviewItem(
            review_item_id="rev_other",
            owner_user_id="other",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Other profile",
            proposal={"profile_delta": {"topic": "Other"}, "source_refs": [{"source_item_id": "src_2"}]},
            status="approved",
        )
    )

    payload = _review_batch_payload(
        store,
        action="apply",
        review_item_ids=[],
        owner_user_id=None,
        review_type="profile_update",
        status="approved",
        limit=10,
        actor_user_id="user_primary",
        reason="batch apply",
        dry_run=False,
    )

    assert payload["summary"] == {"selected": 2, "to_process": 0, "skipped": 2, "affected": 0}
    assert {item["reason"] for item in payload["skipped"]} == {"batch_apply_requires_same_owner_and_review_type"}
    assert store.profile_cards == {}
    assert store.list_audit_events() == []


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
    assert by_type["memory_candidate"]["apply_supported"] is True
    assert by_type["relationship_candidate"]["apply_supported"] is True
    assert by_type["relationship_candidate"]["confidence"] == 0.6
    assert "./scripts/pska review-reject rev_conflict" in by_type["conflict"]["recommended_actions"]


def test_memory_and_profile_list_payloads_are_read_only() -> None:
    store = InMemoryKnowledgeStore()
    verified_at = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    store.add_agent_memory(
        AgentMemory(
            agent_memory_id="agm_1",
            owner_user_id="user_primary",
            layer=MemoryLayer.SEMANTIC,
            text="PSKA prefers deterministic summaries.",
            confidence=0.86,
            source_refs=[SourceRef(source_item_id="src_1", chunk_id="chk_1")],
            last_verified_at=verified_at,
            created_by_user_id="agent_service",
        )
    )
    store.add_agent_memory(
        AgentMemory(
            agent_memory_id="agm_forgotten",
            owner_user_id="user_primary",
            layer=MemoryLayer.EPISODIC,
            text="Old temporary note.",
            confidence=0.0,
            source_refs=[],
            decay_policy="forgotten",
        )
    )
    store.add_profile_card(
        UserProfileCard(
            profile_card_id="upc_1",
            owner_user_id="user_primary",
            profile={"communication": {"style": "concise"}},
            confidence=0.9,
            source_refs=[SourceRef(message_id="msg_profile")],
        )
    )

    before_memory = store.get_agent_memory("agm_1")
    memory_payload = _memory_list_payload(store, owner_user_id="user_primary", limit=10)
    profile_payload = _profile_list_payload(store, owner_user_id="user_primary", limit=10)

    assert memory_payload["read_only"] is True
    assert memory_payload["count"] == 2
    assert memory_payload["agent_memories"][0]["agent_memory_id"] == "agm_1"
    assert memory_payload["agent_memories"][0]["confidence"] == 0.86
    assert memory_payload["agent_memories"][0]["source_refs"] == [{"source_item_id": "src_1", "chunk_id": "chk_1"}]
    assert memory_payload["agent_memories"][0]["last_verified_at"] == verified_at
    assert memory_payload["agent_memories"][0]["status"] == "active"
    assert memory_payload["agent_memories"][0]["promotion_status"] == "updated"
    assert memory_payload["agent_memories"][1]["status"] == "forgotten"
    assert memory_payload["agent_memories"][1]["promotion_status"] == "forgotten"
    assert profile_payload["read_only"] is True
    assert profile_payload["profile_cards"][0]["profile"] == {"communication": {"style": "concise"}}
    assert profile_payload["profile_cards"][0]["source_ref_status"] == "present"
    assert profile_payload["profile_cards"][0]["promotion_status"] == "promoted"
    assert store.get_agent_memory("agm_1") == before_memory


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
            "ready": {"ok": True, "checks": {"agentic_service": {"ok": False}}},
            "metrics": {"index": {"source_items": 0, "entities": 0, "hyperedges": 0}},
            "jobs": {"digest_backlog": {"jobs": 1}},
            "pending_review_items": 0,
        },
        connectors={"state_count": 0},
    )

    assert any("mvp-bootstrap" in action for action in actions)
    assert any("files-scan" in action for action in actions)
    assert any("agentic service" in action for action in actions)


def test_mvp_next_actions_surface_graph_and_digest_work() -> None:
    actions = _mvp_next_actions(
        {
            "ready": {"ok": True, "checks": {"agentic_service": {"ok": True}}},
            "metrics": {"index": {"source_items": 3, "entities": 0, "hyperedges": 0}},
            "jobs": {"digest_backlog": {"jobs": 1}},
            "pending_review_items": 0,
        },
        connectors={"state_count": 1},
    )

    assert any("extract-all" in action for action in actions)
    assert any("adapter worker" in action for action in actions)


def test_mvp_next_actions_ready_state() -> None:
    actions = _mvp_next_actions(
        {
            "ready": {"ok": True, "checks": {"agentic_service": {"ok": True}}},
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
                        "agentic_service": {"ok": True, "provider": "fastreact", "adapter": "fastreact", "pska_tools_loaded": True},
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
    assert summary["agentic_service_pska_tools_loaded"] is True
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
                        "agentic_service": {"ok": False, "provider": "test", "adapter": "fake", "error": "offline"},
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
    assert payload["requires_agentic_service_online"] is False
    assert payload["service_readiness"]["agentic_service_ok"] is False
    assert payload["source_counts"] == {"source_items": 3, "chunks": 4}
    assert payload["digest_backlog"] == {"jobs": 1, "source_items": 1}
    assert payload["pending_reviews"]["total_matching"] == 1
    assert payload["failed_jobs"]["count"] == 1
    assert "./scripts/pska review-list --status pending --owner-user-id user_primary --summary" in payload["recommended_commands"]
    assert "./scripts/pska jobs list --status failed" in payload["recommended_commands"]


def test_daily_briefing_payload_includes_deterministic_next_actions(monkeypatch) -> None:
    created_at = datetime(2026, 6, 17, 8, 30, tzinfo=timezone.utc)

    class FakeStore:
        def list_review_items(self):
            return [
                ReviewItem(
                    review_item_id="rev_1",
                    owner_user_id="user_primary",
                    review_type=ReviewType.CONFLICT,
                    title="Check conflict",
                    proposal={"source_refs": [{"source_item_id": "src_1"}], "confidence": 0.4},
                )
            ]

        def list_source_items(self):
            return [
                SourceItem(
                    source_item_id="src_1",
                    source_channel="manual",
                    record_type="note",
                    source_id="note_1",
                    owner_user_id="user_primary",
                    space_id="private_primary",
                    visibility=Visibility.PRIVATE,
                    visible_team_ids=[],
                    title="Daily source",
                    url=None,
                    content_text="Daily briefing source.",
                    content_hash="hash_1",
                    created_at=created_at,
                )
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
                        "agentic_service": {"ok": False, "provider": "test", "adapter": "fake", "error": "offline"},
                },
            }

        def metrics(self):
            return {
                "index": {"source_items": 1, "chunks": 2, "entities": 1, "hyperedges": 1, "review_items": 1, "jobs": 2},
                "connectors": {
                    "source_channels": {"manual": {"count": 1}},
                    "state_count": 1,
                    "enabled_state_count": 1,
                    "state_sync_status": {"succeeded": 1},
                },
            }

        def job_stats(self):
            return {
                "stats": {
                    "by_status": {"queued": 1, "running": 0, "failed": 1, "succeeded": 0, "canceled": 0},
                    "digest_backlog": {"jobs": 1, "source_items": 1},
                    "running_stale_count": 0,
                    "recent_failed": [{"job_id": "job_1", "job_type": "digest_via_fastreact", "error": "offline"}],
                }
            }

    monkeypatch.setattr(cli_module, "PSKAApi", FakeApi)

    payload = _daily_briefing_payload("postgresql:///example", owner_user_id="user_primary", limit=3)

    assert payload["briefing_type"] == "deterministic_daily_v0"
    assert payload["requires_llm"] is False
    assert payload["requires_agentic_service_online"] is False
    assert payload["service_readiness"]["agentic_service_ok"] is False
    assert payload["source_summary"]["recent_sources"][0]["source_item_id"] == "src_1"
    assert payload["connector_state"]["source_channels"] == ["manual"]
    assert payload["pending_reviews"]["total_matching"] == 1
    assert payload["failed_jobs"]["count"] == 1
    assert "./scripts/pska review-list --status pending --owner-user-id user_primary --summary" in payload["deterministic_next_actions"]
    assert "./scripts/pska jobs list --status failed" in payload["deterministic_next_actions"]
    assert "./scripts/pska fastreact-digest-worker-command" in payload["deterministic_next_actions"]


def test_ops_briefing_payload_distinguishes_recovery_categories(monkeypatch) -> None:
    stale_until = datetime.now(timezone.utc) - timedelta(minutes=5)
    stale_connector_at = datetime.now(timezone.utc) - timedelta(days=3)
    failed_digest = Job(
        job_id="job_digest_failed",
        job_type="digest_via_fastreact",
        payload={},
        status="failed",
        error="FastReAct offline",
    )
    stale_job = Job(
        job_id="job_stale",
        job_type="extract_all",
        payload={},
        status="running",
        worker_id="worker_old",
        leased_until=stale_until,
    )
    connector_state = ConnectorState(
        connector_state_id="conn_files",
        connector_id="files",
        owner_user_id="user_primary",
        sync_status="succeeded",
        last_success_at=stale_connector_at,
    )

    class FakeStore:
        def list_jobs(self, *, status=None, limit=50):
            jobs = [failed_digest, stale_job]
            if status:
                jobs = [job for job in jobs if job.status == status]
            return jobs[:limit]

        def list_connector_states(self, *, owner_user_id=None, connector_id=None):
            return [connector_state]

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
                        "agentic_service": {"ok": False, "provider": "test", "adapter": "fake", "error": "service token required"},
                },
            }

        def metrics(self):
            return {
                "index": {"source_items": 3, "chunks": 4, "jobs": 2},
                "connectors": {
                    "source_channels": {"files": {"count": 3}},
                    "state_count": 1,
                    "enabled_state_count": 1,
                    "state_sync_status": {"succeeded": 1},
                },
            }

        def job_stats(self):
            return {
                "stats": {
                    "by_status": {"queued": 0, "running": 1, "failed": 1, "succeeded": 0, "canceled": 0},
                    "digest_backlog": {"jobs": 0, "source_items": 0},
                    "running_stale_count": 1,
                    "stale_running": [{"job_id": "job_stale", "job_type": "extract_all", "worker_id": "worker_old"}],
                    "recent_failed": [{"job_id": "job_digest_failed", "job_type": "digest_via_fastreact", "error": "FastReAct offline"}],
                }
            }

    monkeypatch.setattr(cli_module, "PSKAApi", FakeApi)

    payload = _ops_briefing_payload("postgresql:///example", owner_user_id="user_primary", limit=3, connector_stale_seconds=3600)
    statuses = {issue["id"]: issue["status"] for issue in payload["issues"]}

    assert payload["ok"] is True
    assert payload["requires_llm"] is False
    assert payload["requires_agentic_service_online"] is False
    assert statuses["service_readiness"] == "ok"
    assert statuses["agentic_service"] == "agentic_service_down"
    assert statuses["stale_jobs"] == "stale_job"
    assert statuses["failed_digest"] == "failed_digest"
    assert statuses["connector_freshness"] == "connector_stale"
    assert statuses["digest_backlog"] == "empty_backlog"
    assert "./scripts/pska job-recover --max-age-seconds 900" in payload["recommended_recovery_commands"]
    assert "./scripts/pska jobs list --status failed --job-type digest_via_fastreact" in payload["recommended_recovery_commands"]
    text = _ops_briefing_text(payload)
    assert "agentic_service_down" in text
    assert "connector_stale" in text


def test_ops_briefing_reports_service_down_without_crashing(monkeypatch) -> None:
    class FakeStore:
        def list_jobs(self, *, status=None, limit=50):
            return []

        def list_connector_states(self, *, owner_user_id=None, connector_id=None):
            return []

    class FakeApi:
        def __init__(self, database_url: str) -> None:
            self.store = FakeStore()

        def ready(self):
            return {
                "ok": False,
                "checks": {
                    "database": {"ok": False, "error": "connection refused"},
                    "schema": {"ok": False},
                    "mcp": {"ok": False},
                    "jobs": {"ok": False},
                    "metrics": {"ok": False},
                    "agentic_service": {"ok": False, "provider": "test", "adapter": "fake"},
                },
            }

        def metrics(self):
            raise RuntimeError("metrics unavailable")

        def job_stats(self):
            raise RuntimeError("jobs unavailable")

    monkeypatch.setattr(cli_module, "PSKAApi", FakeApi)

    payload = _ops_briefing_payload("postgresql:///example")
    service = next(issue for issue in payload["issues"] if issue["id"] == "service_readiness")
    backlog = next(issue for issue in payload["issues"] if issue["id"] == "digest_backlog")

    assert payload["ok"] is False
    assert service["status"] == "service_down"
    assert service["severity"] == "critical"
    assert "database" in service["diagnostics"]["failed_checks"]
    assert backlog["status"] == "empty_backlog"
    assert "./scripts/pska db-init" in payload["recommended_recovery_commands"]


def test_daily_briefing_narrative_saves_fastreact_answer(monkeypatch) -> None:
    created_at = datetime(2026, 6, 17, 8, 30, tzinfo=timezone.utc)
    store = InMemoryKnowledgeStore()
    store.upsert_source_item(
        SourceItem(
            source_item_id="src_1",
            source_channel="manual",
            record_type="note",
            source_id="note_1",
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="Daily source",
            url=None,
            content_text="Daily briefing source.",
            content_hash="hash_1",
            created_at=created_at,
        )
    )

    class FakeApi:
        def __init__(self, database_url: str) -> None:
            assert database_url == "postgresql:///example"
            self.store = store

        def ready(self):
            return {"ok": True, "checks": {"database": {"ok": True}, "schema": {"ok": True}, "mcp": {"ok": True}, "jobs": {"ok": True}, "metrics": {"ok": True}, "agentic_service": {"ok": True, "provider": "test", "adapter": "fake"}}}

        def metrics(self):
            return {
                "index": {"source_items": 1, "chunks": 1, "entities": 1, "hyperedges": 1, "review_items": 0, "jobs": 0},
                "connectors": {"source_channels": {"manual": {}}, "state_count": 1, "enabled_state_count": 1},
            }

        def job_stats(self):
            return {"stats": {"by_status": {"failed": 0}, "digest_backlog": {"jobs": 0, "source_items": 0}, "running_stale_count": 0}}

    class FakeFastreact:
        def chat_completion(self, **kwargs):
            assert kwargs["purpose"] == "pska_narrative_briefing"
            assert kwargs["scope"] == {"source_refs": [{"source_item_id": "src_1"}]}
            prompt = kwargs["messages"][1]["content"]
            assert "source_items=1" in prompt
            assert "recommended_commands" not in prompt
            return {
                "run_id": "run_daily_1",
                "content": "Today PSKA has one fresh source and no urgent review.",
                "source_refs": [{"source_item_id": "src_1"}],
                "tool_calls": [{"name": "pska_index_status"}],
            }

    monkeypatch.setattr(cli_module, "PSKAApi", FakeApi)

    payload = _daily_briefing_payload(
        "postgresql:///example",
        owner_user_id="user_primary",
        limit=3,
        narrative=True,
        fastreact_client=FakeFastreact(),
    )

    saved = store.source_items[payload["narrative"]["saved_source_item_id"]]
    assert payload["narrative"]["ok"] is True
    assert payload["requires_llm"] is True
    assert payload["requires_fastreact_online"] is True
    assert payload["narrative"]["fallback"] is False
    assert payload["narrative"]["source_refs"] == [{"source_item_id": "src_1"}]
    assert payload["narrative"]["trace_summary"]["run_id"] == "run_daily_1"
    assert saved.source_channel == "pska_briefing"
    assert saved.metadata["extra"]["purpose"] == "daily_briefing"
    assert saved.metadata["extra"]["source_refs"] == [{"source_item_id": "src_1"}]
    assert saved.metadata["content"]["trace_summary"]["run_id"] == "run_daily_1"


def test_daily_briefing_narrative_falls_back_when_fastreact_down(monkeypatch) -> None:
    store = InMemoryKnowledgeStore()

    class FakeApi:
        def __init__(self, database_url: str) -> None:
            self.store = store

        def ready(self):
            return {"ok": True, "checks": {"database": {"ok": True}, "schema": {"ok": True}, "mcp": {"ok": True}, "jobs": {"ok": True}, "metrics": {"ok": True}, "agentic_service": {"ok": False, "provider": "test", "adapter": "fake"}}}

        def metrics(self):
            return {
                "index": {"source_items": 0, "chunks": 0, "entities": 0, "hyperedges": 0, "review_items": 0, "jobs": 0},
                "connectors": {"source_channels": {}, "state_count": 0, "enabled_state_count": 0},
            }

        def job_stats(self):
            return {"stats": {"by_status": {"failed": 0}, "digest_backlog": {"jobs": 0, "source_items": 0}, "running_stale_count": 0}}

    class DownFastreact:
        def chat_completion(self, **_kwargs):
            raise FastreactError("FastReAct down")

    monkeypatch.setattr(cli_module, "PSKAApi", FakeApi)

    payload = _daily_briefing_payload(
        "postgresql:///example",
        owner_user_id="user_primary",
        narrative=True,
        narrative_timeout_seconds=75,
        fastreact_client=DownFastreact(),
    )

    assert payload["narrative"]["attempted"] is True
    assert payload["narrative"]["ok"] is False
    assert payload["narrative"]["fallback"] is True
    assert "FastReAct down" in payload["narrative"]["error"]
    assert payload["narrative"]["timeout_seconds"] == 75
    assert payload["deterministic_next_actions"]
    assert not store.source_items


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
