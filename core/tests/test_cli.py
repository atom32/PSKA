from __future__ import annotations

from pska_core.cli import build_parser


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
    service_check = build_parser().parse_args(["service-check", "--url", "http://127.0.0.1:8765", "--timeout-seconds", "1"])
    embed = build_parser().parse_args(["embed-backfill", "--embedding-provider", "bge-m3", "--limit", "10"])
    mcp = build_parser().parse_args(["mcp-server"])

    assert search.command == "search"
    assert search.query == "hello"
    assert search.top_k == 3
    assert smoke.command == "smoke-twitter-import"
    assert agentic.command == "agentic-search"
    assert extract.command == "extract-all"
    assert serve.command == "serve"
    assert service_check.command == "service-check"
    assert service_check.url == "http://127.0.0.1:8765"
    assert service_check.timeout_seconds == 1
    assert embed.command == "embed-backfill"
    assert embed.embedding_provider == "bge-m3"
    assert embed.limit == 10
    assert mcp.command == "mcp-server"
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
    assert args.force is True
    assert args.reason == "new import"
