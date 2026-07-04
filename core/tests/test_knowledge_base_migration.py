from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

import pska_core.cli as cli_module


def test_knowledge_base_migration_backfills_legacy_sources_and_is_idempotent() -> None:
    psql = _psql_or_skip()
    _require_pgvector_or_skip(psql)
    createdb = _sibling_tool_or_skip(psql, "createdb")
    dropdb = _sibling_tool_or_skip(psql, "dropdb")
    database_name = f"pska_kb_migration_{uuid.uuid4().hex[:12]}"
    database_url = f"postgresql:///{database_name}"

    create_result = subprocess.run([createdb, database_name], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if create_result.returncode != 0:
        pytest.skip(f"temporary PostgreSQL database could not be created: {create_result.stderr.strip()}")

    try:
        _apply_migrations(psql, database_url, through="020_")
        _seed_legacy_corpus(database_url)
        _apply_single_migration(psql, database_url, "021_knowledge_bases.sql")
        first_counts = _knowledge_base_counts(database_url)
        _apply_single_migration(psql, database_url, "021_knowledge_bases.sql")

        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select knowledge_base_id, slug, name, default_space_id
                    from knowledge_bases
                    where tenant_id = 'tenant_migration'
                      and owner_user_id = 'legacy_user'
                      and is_default = true
                    """
                )
                default_kbs = cur.fetchall()
                assert len(default_kbs) == 1
                default_kb = default_kbs[0]
                assert default_kb["slug"] == "default"
                assert default_kb["name"] == "默认资料库"
                assert default_kb["default_space_id"] == "legacy_private"

                cur.execute(
                    """
                    select knowledge_source_id, membership_status, metadata->>'backfilled' as backfilled
                    from knowledge_base_sources
                    where knowledge_base_id = %s
                    order by knowledge_source_id
                    """,
                    (default_kb["knowledge_base_id"],),
                )
                source_memberships = [dict(row) for row in cur.fetchall()]
                assert source_memberships == [
                    {"knowledge_source_id": "ks_legacy_active", "membership_status": "active", "backfilled": "true"},
                    {"knowledge_source_id": "ks_legacy_deleted", "membership_status": "archived", "backfilled": "true"},
                ]

                cur.execute(
                    """
                    select source_item_id, membership_type, membership_status, metadata->>'backfilled' as backfilled
                    from knowledge_base_source_items
                    where knowledge_base_id = %s
                    order by source_item_id
                    """,
                    (default_kb["knowledge_base_id"],),
                )
                item_memberships = [dict(row) for row in cur.fetchall()]
                assert item_memberships == [
                    {"source_item_id": "src_legacy_active", "membership_type": "backfill", "membership_status": "active", "backfilled": "true"},
                    {"source_item_id": "src_legacy_deleted", "membership_type": "backfill", "membership_status": "archived", "backfilled": "true"},
                ]

        assert _knowledge_base_counts(database_url) == first_counts
    finally:
        subprocess.run([dropdb, "--if-exists", database_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _psql_or_skip() -> str:
    try:
        return cli_module._psql_path()
    except SystemExit as exc:
        pytest.skip(str(exc))


def _sibling_tool_or_skip(psql: str, tool_name: str) -> str:
    candidate = Path(psql).with_name(tool_name)
    if candidate.exists():
        return str(candidate)
    found = shutil.which(tool_name)
    if found:
        return found
    pytest.skip(f"{tool_name} not found. Add PostgreSQL tools to PATH.")


def _require_pgvector_or_skip(psql: str) -> None:
    result = subprocess.run(
        [psql, "postgres", "-Atc", "select 1 from pg_available_extensions where name = 'vector';"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"PostgreSQL is not reachable: {result.stderr.strip()}")
    if result.stdout.strip() != "1":
        pytest.skip("pgvector extension is not available in local PostgreSQL")


def _apply_migrations(psql: str, database_url: str, *, through: str) -> None:
    migration_dir = Path(__file__).resolve().parents[1] / "src" / "pska_core" / "migrations"
    for migration in sorted(migration_dir.glob("*.sql")):
        if migration.name.startswith(through):
            _apply_migration_path(psql, database_url, migration)
            return
        _apply_migration_path(psql, database_url, migration)
    raise AssertionError(f"Migration prefix {through} was not found")


def _apply_single_migration(psql: str, database_url: str, migration_name: str) -> None:
    migration = Path(__file__).resolve().parents[1] / "src" / "pska_core" / "migrations" / migration_name
    _apply_migration_path(psql, database_url, migration)


def _apply_migration_path(psql: str, database_url: str, migration: Path) -> None:
    result = subprocess.run(
        [psql, database_url, "-v", "ON_ERROR_STOP=1", "-f", str(migration)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"{migration.name} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


def _seed_legacy_corpus(database_url: str) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("insert into tenants(tenant_id, slug, name) values ('tenant_migration', 'tenant-migration', 'Migration Tenant')")
            cur.execute(
                """
                insert into users(user_id, tenant_id, handle, role)
                values ('legacy_user', 'tenant_migration', 'legacy-user', 'user')
                """
            )
            cur.execute(
                """
                insert into spaces(space_id, tenant_id, slug, kind, owner_user_id)
                values ('legacy_private', 'tenant_migration', 'legacy-private', 'private', 'legacy_user')
                """
            )
            cur.execute(
                """
                insert into knowledge_sources(
                  knowledge_source_id,
                  tenant_id,
                  owner_user_id,
                  name,
                  source_type,
                  uri,
                  status,
                  connector_id,
                  space_id,
                  visibility
                )
                values
                  ('ks_legacy_active', 'tenant_migration', 'legacy_user', 'Legacy Active Source', 'folder', 'file:///legacy-active', 'authorized', 'files', 'legacy_private', 'private'),
                  ('ks_legacy_deleted', 'tenant_migration', 'legacy_user', 'Legacy Deleted Source', 'folder', 'file:///legacy-deleted', 'deleted', 'files', 'legacy_private', 'private')
                """
            )
            cur.execute(
                """
                insert into source_items(
                  source_item_id,
                  tenant_id,
                  source_channel,
                  record_type,
                  source_id,
                  owner_user_id,
                  space_id,
                  visibility,
                  title,
                  content_text,
                  content_hash,
                  metadata,
                  lifecycle_status,
                  deleted_at
                )
                values
                  ('src_legacy_active', 'tenant_migration', 'files', 'document', 'ks_legacy_active', 'legacy_user', 'legacy_private', 'private', 'Legacy Active Doc', 'active legacy content', 'hash-legacy-active', %s, 'active', null),
                  ('src_legacy_deleted', 'tenant_migration', 'files', 'document', 'ks_legacy_deleted', 'legacy_user', 'legacy_private', 'private', 'Legacy Deleted Doc', 'deleted legacy content', 'hash-legacy-deleted', %s, 'deleted', now())
                """,
                (Jsonb({}), Jsonb({})),
            )


def _knowledge_base_counts(database_url: str) -> dict[str, int]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from knowledge_bases")
            knowledge_bases = cur.fetchone()[0]
            cur.execute("select count(*) from knowledge_base_sources")
            sources = cur.fetchone()[0]
            cur.execute("select count(*) from knowledge_base_source_items")
            source_items = cur.fetchone()[0]
    return {"knowledge_bases": knowledge_bases, "knowledge_base_sources": sources, "knowledge_base_source_items": source_items}
