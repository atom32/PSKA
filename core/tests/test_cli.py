from __future__ import annotations

from pska_core.cli import build_parser


def test_cli_accepts_db_check() -> None:
    args = build_parser().parse_args(["db-check"])

    assert args.command == "db-check"
    assert args.database_url == "postgresql:///pska"


def test_cli_accepts_database_url_override() -> None:
    args = build_parser().parse_args(["--database-url", "postgresql:///example", "db-init"])

    assert args.command == "db-init"
    assert args.database_url == "postgresql:///example"


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
    embed = build_parser().parse_args(["embed-backfill", "--embedding-provider", "bge-m3", "--limit", "10"])
    mcp = build_parser().parse_args(["mcp-server"])

    assert search.command == "search"
    assert search.query == "hello"
    assert search.top_k == 3
    assert smoke.command == "smoke-twitter-import"
    assert agentic.command == "agentic-search"
    assert extract.command == "extract-all"
    assert serve.command == "serve"
    assert embed.command == "embed-backfill"
    assert embed.embedding_provider == "bge-m3"
    assert embed.limit == 10
    assert mcp.command == "mcp-server"
