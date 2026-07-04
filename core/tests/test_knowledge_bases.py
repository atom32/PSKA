from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from pska_core.acl import ACLService
from pska_core.api import PSKAApi
from pska_core.auth import RequestContext
from pska_core.config import AuthConfig, PSKAConfig, ServiceConfig
from pska_core.enums import ReviewType, UserRole, Visibility
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.models import (
    Chunk,
    DigestNote,
    Document,
    KnowledgeBaseSourceItem,
    KnowledgeClaim,
    OfflineIndexState,
    ProcessingSpan,
    ReviewItem,
    SourceItem,
    SourceRef,
    SyncRun,
    User,
)
from pska_core.retrieval import RetrievalService
from pska_core.store import InMemoryKnowledgeStore, default_knowledge_base_id


TENANT_A = "tenant_a"
TENANT_B = "tenant_b"


def test_default_knowledge_base_is_idempotent_and_owner_scoped() -> None:
    store = InMemoryKnowledgeStore()

    kb_a = store.ensure_default_knowledge_base(tenant_id=TENANT_A, owner_user_id="user_a")
    kb_a_again = store.ensure_default_knowledge_base(tenant_id=TENANT_A, owner_user_id="user_a")
    kb_b = store.ensure_default_knowledge_base(tenant_id=TENANT_B, owner_user_id="user_a")

    assert kb_a.knowledge_base_id == default_knowledge_base_id(TENANT_A, "user_a")
    assert kb_a_again.knowledge_base_id == kb_a.knowledge_base_id
    assert kb_b.knowledge_base_id == default_knowledge_base_id(TENANT_B, "user_a")
    assert kb_b.knowledge_base_id != kb_a.knowledge_base_id
    assert [kb.knowledge_base_id for kb in store.list_knowledge_bases(tenant_id=TENANT_A, owner_user_id="user_a")] == [kb_a.knowledge_base_id]
    assert [kb.knowledge_base_id for kb in store.list_knowledge_bases(tenant_id=TENANT_B, owner_user_id="user_a")] == [kb_b.knowledge_base_id]


def test_knowledge_base_source_item_membership_filters_by_kb_and_tenant() -> None:
    store = InMemoryKnowledgeStore()
    kb_a = store.ensure_default_knowledge_base(tenant_id=TENANT_A, owner_user_id="user_a")
    kb_b = store.ensure_default_knowledge_base(tenant_id=TENANT_B, owner_user_id="user_b")
    item_a = _source_item("item_a", tenant_id=TENANT_A, owner_user_id="user_a")
    item_b = _source_item("item_b", tenant_id=TENANT_B, owner_user_id="user_b")
    store.upsert_source_item(item_a)
    store.upsert_source_item(item_b)

    store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=kb_a.knowledge_base_id,
            source_item_id=item_a.source_item_id,
            tenant_id=TENANT_A,
            owner_user_id="user_a",
            added_by_user_id="user_a",
        )
    )
    store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=kb_b.knowledge_base_id,
            source_item_id=item_b.source_item_id,
            tenant_id=TENANT_B,
            owner_user_id="user_b",
            added_by_user_id="user_b",
        )
    )

    assert store.list_knowledge_base_source_item_ids({kb_a.knowledge_base_id}, tenant_id=TENANT_A, owner_user_id="user_a") == {"item_a"}
    assert store.list_knowledge_base_source_item_ids({kb_a.knowledge_base_id}, tenant_id=TENANT_B, owner_user_id="user_b") == set()

    store.update_source_lifecycle(
        ["item_a"],
        lifecycle_status="deleted",
        actor_user_id="user_a",
        reason="test cleanup",
        tenant_id=TENANT_A,
    )

    assert store.list_knowledge_base_source_item_ids({kb_a.knowledge_base_id}, tenant_id=TENANT_A, owner_user_id="user_a") == set()
    assert store.list_knowledge_base_source_item_ids({kb_a.knowledge_base_id}, tenant_id=TENANT_A, owner_user_id="user_a", active_only=False) == {"item_a"}


def test_workspace_knowledge_base_api_crud_returns_counts() -> None:
    api = _api()
    context = RequestContext()

    listing = api.workspace_knowledge_bases(context=context)
    created = api.create_workspace_knowledge_base({"name": "Research Notes", "description": "Evidence corpus."}, context=context)
    knowledge_base_id = created["knowledge_base"]["knowledge_base_id"]
    item = _source_item("source_item_1")
    api.store.upsert_source_item(item)
    api.store.add_document(
        Document(
            document_id="doc_1",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            title=item.title,
            body=item.content_text,
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_chunk(
        Chunk(
            chunk_id="chunk_1",
            document_id="doc_1",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            text=item.content_text,
            embedding=[1.0, 0.0],
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=knowledge_base_id,
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            added_by_user_id=item.owner_user_id,
            tenant_id=item.tenant_id,
        )
    )

    detail = api.workspace_knowledge_base(knowledge_base_id, context=context)
    updated = api.update_workspace_knowledge_base(knowledge_base_id, {"name": "Updated Notes", "pinned": True}, context=context)
    deleted = api.delete_workspace_knowledge_base(knowledge_base_id, context=context)
    after_delete = api.workspace_knowledge_bases(context=context)

    assert listing["default_knowledge_base_id"]
    assert created["knowledge_base"]["slug"] == "research-notes"
    assert detail["knowledge_base"]["counts"]["source_items"] == 1
    assert detail["knowledge_base"]["counts"]["documents"] == 1
    assert detail["knowledge_base"]["counts"]["chunks"] == 1
    assert detail["knowledge_base"]["counts"]["active_chunks"] == 1
    assert detail["knowledge_base"]["counts"]["embedded_chunks"] == 1
    assert detail["knowledge_base"]["readiness"]["retrieval_ready"] is True
    assert detail["knowledge_base"]["readiness"]["embedding_coverage"] == 1.0
    assert detail["knowledge_base"]["source_item_ids"] == ["source_item_1"]
    assert updated["knowledge_base"]["name"] == "Updated Notes"
    assert updated["knowledge_base"]["pinned_at"] is not None
    assert deleted["knowledge_base"]["status"] == "archived"
    assert knowledge_base_id not in [kb["knowledge_base_id"] for kb in after_delete["knowledge_bases"]]


def test_workspace_knowledge_base_pin_unpin_and_restore_keeps_manual_archives() -> None:
    api = _api()
    context = RequestContext()
    created = api.create_workspace_knowledge_base({"name": "Pinned Restore KB"}, context=context)
    knowledge_base_id = created["knowledge_base"]["knowledge_base_id"]
    active_item = _source_item("active_restore_item")
    manually_archived_item = _source_item("manual_archive_item")
    api.store.upsert_source_item(active_item)
    api.store.upsert_source_item(manually_archived_item)
    for item in [active_item, manually_archived_item]:
        api.store.add_knowledge_base_source_item(
            KnowledgeBaseSourceItem(
                knowledge_base_id=knowledge_base_id,
                source_item_id=item.source_item_id,
                owner_user_id=item.owner_user_id,
                added_by_user_id=item.owner_user_id,
                tenant_id=item.tenant_id,
            )
        )
    api.store.archive_knowledge_base_source_items(
        knowledge_base_id,
        [manually_archived_item.source_item_id],
        tenant_id="tenant_default",
        owner_user_id="user_primary",
        actor_user_id="user_primary",
        reason="manual archive before kb archive",
    )

    pinned = api.pin_workspace_knowledge_base(knowledge_base_id, context=context)
    unpinned = api.pin_workspace_knowledge_base(knowledge_base_id, context=context, pinned=False)
    archived = api.delete_workspace_knowledge_base(knowledge_base_id, context=context)
    archived_listing = api.workspace_knowledge_bases({"include_archived": True}, context=context)
    restored = api.restore_workspace_knowledge_base(knowledge_base_id, context=context)
    active_listing = api.workspace_knowledge_bases(context=context)
    after_restore = api.workspace_knowledge_base(knowledge_base_id, context=context)

    assert pinned["knowledge_base"]["pinned_at"] is not None
    assert unpinned["knowledge_base"]["pinned_at"] is None
    assert archived["knowledge_base"]["status"] == "archived"
    assert knowledge_base_id in [item["knowledge_base_id"] for item in archived_listing["knowledge_bases"]]
    assert archived_listing["include_deleted"] is True
    assert restored["knowledge_base"]["status"] == "active"
    assert restored["knowledge_base"]["deleted_at"] is None
    assert knowledge_base_id in [item["knowledge_base_id"] for item in active_listing["knowledge_bases"]]
    assert after_restore["knowledge_base"]["source_item_ids"] == [active_item.source_item_id]


def test_workspace_knowledge_base_readiness_explains_processing_embedding_and_lineage() -> None:
    api = _api()
    context = RequestContext()
    created = api.create_workspace_knowledge_base({"name": "Ready Corpus"}, context=context)
    knowledge_base_id = created["knowledge_base"]["knowledge_base_id"]
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    item = _source_item("source_item_ready")
    api.store.upsert_source_item(item)
    api.store.add_document(
        Document(
            document_id="doc_ready",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            title=item.title,
            body=item.content_text,
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_chunk(
        Chunk(
            chunk_id="chunk_embedded",
            document_id="doc_ready",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            text="embedded chunk",
            embedding=[0.1, 0.2],
            metadata={"embedding_provider": "test", "embedding_model": "mini"},
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_chunk(
        Chunk(
            chunk_id="chunk_missing",
            document_id="doc_ready",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            text="missing embedding chunk",
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=knowledge_base_id,
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            added_by_user_id=item.owner_user_id,
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_sync_run(
        SyncRun(
            sync_run_id="sync_ready",
            knowledge_source_id=item.source_id,
            owner_user_id=item.owner_user_id,
            connector_id="manual",
            status="succeeded",
            started_at=now,
            finished_at=now + timedelta(minutes=1),
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_processing_span(
        ProcessingSpan(
            processing_span_id="span_failed",
            knowledge_source_id=item.source_id,
            owner_user_id=item.owner_user_id,
            stage="extract",
            status="failed",
            started_at=now + timedelta(minutes=2),
            finished_at=now + timedelta(minutes=3),
            source_item_id=item.source_item_id,
            error="parser failed",
            tenant_id=item.tenant_id,
        )
    )
    api.store.upsert_offline_index_state(
        OfflineIndexState(
            object_type="chunk",
            object_id="chunk_missing",
            owner_user_id=item.owner_user_id,
            source_item_id=item.source_item_id,
            status="dirty",
            tenant_id=item.tenant_id,
            updated_at=now + timedelta(minutes=4),
        )
    )
    api.store.add_digest_note(
        DigestNote(
            digest_note_id="digest_ready",
            owner_user_id=item.owner_user_id,
            title="Ready digest",
            synopsis="digest",
            source_refs=[SourceRef(source_item_id=item.source_item_id)],
            created_at=now + timedelta(minutes=5),
            tenant_id=item.tenant_id,
        )
    )

    detail = api.workspace_knowledge_base(knowledge_base_id, context=context)["knowledge_base"]
    counts = detail["counts"]
    readiness = detail["readiness"]

    assert counts["chunks"] == 2
    assert counts["embedded_chunks"] == 1
    assert counts["failed_processing_spans"] == 1
    assert counts["offline_index_dirty"] == 1
    assert readiness["processing_status"] == "failed"
    assert readiness["embedding_coverage"] == 0.5
    assert readiness["embedding_status"] == "partial"
    assert readiness["embedding_models"] == ["test/mini"]
    assert readiness["failed_processing_count"] == 1
    assert readiness["offline_index_fresh"] is False
    assert readiness["last_sync_at"] == (now + timedelta(minutes=1)).isoformat()
    assert readiness["last_digest_at"] == (now + timedelta(minutes=5)).isoformat()
    assert readiness["last_error"] == "parser failed"


def test_text_source_binds_to_explicit_knowledge_base() -> None:
    api = _api()
    context = RequestContext()
    created = api.create_workspace_knowledge_base({"name": "Scoped Corpus"}, context=context)
    knowledge_base_id = created["knowledge_base"]["knowledge_base_id"]

    response = api.create_text_source(
        {
            "title": "Scoped note",
            "text": "This note belongs to the scoped corpus.",
            "knowledge_base_id": knowledge_base_id,
            "digest_mode": "manual",
        },
        context=context,
    )

    source_item_ids = set(response["source_item_ids"])
    assert response["knowledge_base_ids"] == [knowledge_base_id]
    assert api.store.list_knowledge_base_source_item_ids(
        {knowledge_base_id},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == source_item_ids


def test_upload_source_binds_to_explicit_knowledge_base() -> None:
    api = _api()
    context = RequestContext()
    created = api.create_workspace_knowledge_base({"name": "Upload Corpus"}, context=context)
    knowledge_base_id = created["knowledge_base"]["knowledge_base_id"]

    response = api.create_upload_source(
        {
            "filename": "field-notes.md",
            "bytes_base64": base64.b64encode(b"# Field notes\nuploadscopedtoken belongs to this uploaded file.").decode("ascii"),
            "knowledge_base_id": knowledge_base_id,
            "digest_mode": "manual",
        },
        context=context,
    )

    source_item_ids = set(response["source_item_ids"])
    documents = api.workspace_documents_data({"knowledge_base_id": knowledge_base_id}, context=context)

    assert response["action"] == "upload"
    assert response["knowledge_base_ids"] == [knowledge_base_id]
    assert api.store.list_knowledge_base_source_item_ids(
        {knowledge_base_id},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == source_item_ids
    assert {document["source_item_id"] for document in documents["documents"]} == source_item_ids


def test_source_sync_uses_existing_knowledge_base_membership(tmp_path) -> None:
    api = _api()
    context = RequestContext()
    created = api.create_workspace_knowledge_base({"name": "Folder Corpus"}, context=context)
    knowledge_base_id = created["knowledge_base"]["knowledge_base_id"]
    (tmp_path / "alpha.txt").write_text("foldersynctoken belongs to the synced folder source.", encoding="utf-8")

    source = api.create_knowledge_source(
        {
            "source_type": "folder",
            "path": str(tmp_path),
            "name": "Scoped folder",
            "knowledge_base_id": knowledge_base_id,
        },
        context=context,
    )
    knowledge_source_id = source["knowledge_source"]["knowledge_source_id"]

    synced = api.sync_knowledge_sources(
        {
            "knowledge_source_id": knowledge_source_id,
            "limit": 10,
        },
        context=context,
    )
    source_item_ids = api.store.list_knowledge_base_source_item_ids(
        {knowledge_base_id},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    )
    documents = api.workspace_documents_data({"knowledge_base_id": knowledge_base_id}, context=context)

    assert source["knowledge_base_ids"] == [knowledge_base_id]
    assert api.store.list_knowledge_base_ids_for_source(
        knowledge_source_id,
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == {knowledge_base_id}
    assert synced["ok"] is True
    assert synced["totals"]["ingested"] == 1
    assert len(source_item_ids) == 1
    assert {document["source_item_id"] for document in documents["documents"]} == source_item_ids


def test_files_sync_binds_source_items_to_explicit_knowledge_base(tmp_path) -> None:
    api = _api()
    context = RequestContext(tenant_id="tenant_graphintell", user_id="authnode_user", represented_user_id="test_user")
    created = api.create_workspace_knowledge_base({"name": "Files Sync Corpus"}, context=context)
    knowledge_base_id = created["knowledge_base"]["knowledge_base_id"]
    (tmp_path / "notes.txt").write_text("filessynctoken belongs to this local folder.", encoding="utf-8")

    synced = api.files_sync(
        {
            "roots": [str(tmp_path)],
            "knowledge_base_id": knowledge_base_id,
            "skip_twitter_archives": True,
        },
        context=context,
    )
    source = synced["knowledge_sources"][0]
    knowledge_source_id = source["knowledge_source_id"]
    source_item_ids = api.store.list_knowledge_base_source_item_ids(
        {knowledge_base_id},
        tenant_id=context.tenant_id,
        owner_user_id=context.represented_user_id,
    )
    documents = api.workspace_documents_data({"knowledge_base_id": knowledge_base_id}, context=context)

    assert synced["ok"] is True
    assert synced["totals"]["ingested"] == 1
    assert source["tenant_id"] == context.tenant_id
    assert source["owner_user_id"] == context.represented_user_id
    assert api.store.list_knowledge_base_ids_for_source(
        knowledge_source_id,
        tenant_id=context.tenant_id,
        owner_user_id=context.represented_user_id,
    ) == {knowledge_base_id}
    assert len(source_item_ids) == 1
    assert {document["source_item_id"] for document in documents["documents"]} == source_item_ids


def test_workspace_documents_link_adds_existing_source_to_target_kb() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    source = api.create_text_source(
        {
            "title": "Reusable note",
            "text": "This reusable evidence can be linked into another knowledge base.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = source["source_item_ids"][0]

    preview = api.workspace_documents_link(
        {
            "source_item_ids": [source_item_id],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
        },
        context=context,
    )
    linked = api.workspace_documents_link(
        {
            "source_item_ids": [source_item_id],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )
    linked_again = api.workspace_documents_link(
        {
            "source_item_ids": [source_item_id],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )
    beta_documents = api.workspace_documents_data(
        {"knowledge_base_id": beta_kb["knowledge_base_id"], "include_deleted": True},
        context=context,
    )

    assert preview["dry_run"] is True
    assert preview["counts"]["new"] == 1
    assert preview["counts"]["knowledge_base_source_items"] == 1
    assert linked["execute"] is True
    assert linked["linked"]["new"] == 1
    assert linked["linked"]["already_present"] == 0
    assert linked_again["linked"]["new"] == 0
    assert linked_again["linked"]["already_present"] == 1
    assert api.store.list_knowledge_base_source_item_ids(
        {beta_kb["knowledge_base_id"]},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == {source_item_id}
    assert [document["source_item_id"] for document in beta_documents["documents"]] == [source_item_id]
    assert set(beta_documents["documents"][0]["knowledge_base_ids"]) == {
        alpha_kb["knowledge_base_id"],
        beta_kb["knowledge_base_id"],
    }
    assert set(beta_documents["documents"][0]["knowledge_base_names"]) == {"Alpha KB", "Beta KB"}


def test_workspace_documents_link_reactivates_archived_membership_but_not_deleted_source() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    source = api.create_text_source(
        {
            "title": "Relinkable note",
            "text": "This evidence can be removed from a target knowledge base and linked again.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = source["source_item_ids"][0]
    api.workspace_documents_link(
        {
            "source_item_ids": [source_item_id],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )
    api.workspace_documents_delete(
        {
            "source_item_ids": [source_item_id],
            "knowledge_base_id": beta_kb["knowledge_base_id"],
            "delete_mode": "membership",
            "execute": True,
        },
        context=context,
    )

    relinked = api.workspace_documents_link(
        {
            "source_item_ids": [source_item_id],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )

    assert relinked["linked"]["reactivated"] == 1
    assert relinked["linked"]["new"] == 0
    assert api.store.list_knowledge_base_source_item_ids(
        {beta_kb["knowledge_base_id"]},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == {source_item_id}
    api.workspace_documents_delete(
        {
            "source_item_ids": [source_item_id],
            "delete_mode": "source",
            "execute": True,
        },
        context=context,
    )
    try:
        api.workspace_documents_link(
            {
                "source_item_ids": [source_item_id],
                "target_knowledge_base_id": beta_kb["knowledge_base_id"],
                "execute": True,
            },
            context=context,
        )
    except PermissionError as exc:
        assert "no active owned document entries matched" in str(exc)
    else:
        raise AssertionError("deleted source item should not be linked to a knowledge base")


def test_workspace_documents_move_archives_source_membership_after_target_link() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    source = api.create_text_source(
        {
            "title": "Movable note",
            "text": "This evidence should move from one knowledge base to another without being soft deleted.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = source["source_item_ids"][0]

    preview = api.workspace_documents_move(
        {
            "source_item_ids": [source_item_id],
            "source_knowledge_base_id": alpha_kb["knowledge_base_id"],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
        },
        context=context,
    )
    before_alpha = api.workspace_documents_data({"knowledge_base_id": alpha_kb["knowledge_base_id"]}, context=context)
    before_beta = api.workspace_documents_data({"knowledge_base_id": beta_kb["knowledge_base_id"]}, context=context)
    moved = api.workspace_documents_move(
        {
            "source_item_ids": [source_item_id],
            "source_knowledge_base_id": alpha_kb["knowledge_base_id"],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )
    after_alpha = api.workspace_documents_data({"knowledge_base_id": alpha_kb["knowledge_base_id"], "include_deleted": True}, context=context)
    after_beta = api.workspace_documents_data({"knowledge_base_id": beta_kb["knowledge_base_id"], "include_deleted": True}, context=context)
    moved_item = next(item for item in api.store.list_source_items(tenant_id=context.tenant_id) if item.source_item_id == source_item_id)

    assert preview["dry_run"] is True
    assert preview["counts"]["moved"] == 1
    assert preview["counts"]["new"] == 1
    assert [document["source_item_id"] for document in before_alpha["documents"]] == [source_item_id]
    assert before_beta["documents"] == []
    assert moved["moved"]["moved"] == 1
    assert moved["moved"]["archived_source_memberships"] == 1
    assert moved["moved"]["orphan_source_items"] == 0
    assert moved_item.lifecycle_status == "active"
    assert api.store.list_knowledge_base_source_item_ids(
        {alpha_kb["knowledge_base_id"]},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == set()
    assert api.store.list_knowledge_base_source_item_ids(
        {beta_kb["knowledge_base_id"]},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == {source_item_id}
    assert after_alpha["documents"] == []
    assert [document["source_item_id"] for document in after_beta["documents"]] == [source_item_id]
    assert after_beta["documents"][0]["knowledge_base_ids"] == [beta_kb["knowledge_base_id"]]
    assert after_beta["documents"][0]["knowledge_base_names"] == ["Beta KB"]


def test_workspace_documents_move_to_existing_target_membership_is_not_a_copy() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    source = api.create_text_source(
        {
            "title": "Already linked note",
            "text": "This evidence is already linked to the target before it is moved.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = source["source_item_ids"][0]
    api.workspace_documents_link(
        {
            "source_item_ids": [source_item_id],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )

    moved = api.workspace_documents_move(
        {
            "source_item_ids": [source_item_id],
            "source_knowledge_base_id": alpha_kb["knowledge_base_id"],
            "target_knowledge_base_id": beta_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )

    assert moved["moved"]["already_present"] == 1
    assert moved["moved"]["new"] == 0
    assert api.store.list_knowledge_base_source_item_ids(
        {alpha_kb["knowledge_base_id"]},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == set()
    assert api.store.list_knowledge_base_source_item_ids(
        {beta_kb["knowledge_base_id"]},
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
    ) == {source_item_id}


def test_workspace_document_delete_removes_current_kb_membership_without_deleting_shared_source() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    source = api.create_text_source(
        {
            "title": "Shared note",
            "text": "shared corpus evidence stays alive in another knowledge base.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = source["source_item_ids"][0]
    api.store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=beta_kb["knowledge_base_id"],
            source_item_id=source_item_id,
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
            added_by_user_id=context.user_id,
        )
    )

    preview = api.workspace_documents_delete(
        {
            "source_item_ids": [source_item_id],
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "execute": False,
        },
        context=context,
    )
    deleted = api.workspace_documents_delete(
        {
            "source_item_ids": [source_item_id],
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )

    source_item = api.store.source_items[source_item_id]
    assert preview["delete_mode"] == "membership"
    assert preview["counts"]["knowledge_base_source_items"] == 1
    assert preview["counts"]["orphan_source_items"] == 0
    assert deleted["deleted"]["knowledge_base_source_items"] == 1
    assert source_item.lifecycle_status == "active"
    assert api.store.list_knowledge_base_source_item_ids({alpha_kb["knowledge_base_id"]}, tenant_id=context.tenant_id, owner_user_id=context.user_id) == set()
    assert api.store.list_knowledge_base_source_item_ids({beta_kb["knowledge_base_id"]}, tenant_id=context.tenant_id, owner_user_id=context.user_id) == {source_item_id}
    alpha_documents = api.workspace_documents_data(
        {"knowledge_base_id": alpha_kb["knowledge_base_id"], "include_deleted": True},
        context=context,
    )
    beta_documents = api.workspace_documents_data(
        {"knowledge_base_id": beta_kb["knowledge_base_id"], "include_deleted": True},
        context=context,
    )
    assert source_item_id not in {document["source_item_id"] for document in alpha_documents["documents"]}
    assert source_item_id in {document["source_item_id"] for document in beta_documents["documents"]}


def test_workspace_document_delete_soft_deletes_orphan_after_membership_remove() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    source = api.create_text_source(
        {
            "title": "Single KB note",
            "text": "single corpus evidence should soft delete when no membership remains.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = source["source_item_ids"][0]

    deleted = api.workspace_documents_delete(
        {
            "source_item_ids": [source_item_id],
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )

    source_item = api.store.source_items[source_item_id]
    assert deleted["delete_mode"] == "membership"
    assert deleted["deleted"]["knowledge_base_source_items"] == 1
    assert deleted["deleted"]["orphan_source_items"] == 1
    assert deleted["deleted"]["source_items"] == 1
    assert source_item.lifecycle_status == "deleted"
    assert api.store.list_knowledge_base_source_item_ids({alpha_kb["knowledge_base_id"]}, tenant_id=context.tenant_id, owner_user_id=context.user_id) == set()
    alpha_documents = api.workspace_documents_data(
        {"knowledge_base_id": alpha_kb["knowledge_base_id"], "include_deleted": True},
        context=context,
    )
    matching = [document for document in alpha_documents["documents"] if document["source_item_id"] == source_item_id]
    assert matching and matching[0]["lifecycle_status"] == "deleted"


def test_workspace_ask_knowledge_base_scope_filters_rag_evidence() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    alpha = api.create_text_source(
        {
            "title": "Alpha note",
            "text": "sharedtoken belongs to alpha evidence only.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    beta = api.create_text_source(
        {
            "title": "Beta note",
            "text": "sharedtoken belongs to beta evidence only.",
            "knowledge_base_id": beta_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )

    response = api.workspace_ask(
        {
            "query": "sharedtoken",
            "intent": "kb_search",
            "skip_intent_classifier": True,
            "scope": {"knowledge_base_ids": [alpha_kb["knowledge_base_id"]]},
        },
        context=context,
    )

    assert response["route"]["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert response["route"]["scope_applied"]["source_item_ids"] == alpha["source_item_ids"]
    assert {result["source_item_id"] for result in response["evidence"]["results"]} == set(alpha["source_item_ids"])
    readiness = response["route"]["scope_applied"]["knowledge_base_readiness"][0]
    assert readiness["knowledge_base_id"] == alpha_kb["knowledge_base_id"]
    assert readiness["source_item_count"] == 1
    assert readiness["retrieval_ready"] is True
    assert response["route"]["scope_applied"]["knowledge_base_readiness_warnings"] == []

    empty_intersection = api.workspace_ask(
        {
            "query": "sharedtoken",
            "intent": "kb_search",
            "skip_intent_classifier": True,
            "scope": {
                "knowledge_base_ids": [alpha_kb["knowledge_base_id"]],
                "source_item_ids": beta["source_item_ids"],
            },
        },
        context=context,
    )

    assert empty_intersection["route"]["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert empty_intersection["route"]["scope_applied"]["source_item_ids"] == []
    assert empty_intersection["route"]["scope_applied"]["dropped_scope_ids"] == beta["source_item_ids"]
    assert empty_intersection["route"]["scope_applied"]["dropped_source_item_ids"] == beta["source_item_ids"]
    assert empty_intersection["evidence"]["results"] == []
    assert empty_intersection["quality_signals"]["no_answer_diagnostics"]["primary_reason"] == "selected_scope_empty"
    assert "selected_scope_empty" in empty_intersection["quality_signals"]["flags"]


def test_workspace_knowledge_base_search_returns_scoped_attributed_results() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha Search KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta Search KB"}, context=context)["knowledge_base"]
    alpha = api.create_text_source(
        {
            "title": "Alpha search note",
            "text": "scopedsearchtoken belongs to alpha search evidence only.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    beta = api.create_text_source(
        {
            "title": "Beta search note",
            "text": "scopedsearchtoken belongs to beta search evidence only.",
            "knowledge_base_id": beta_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )

    response = api.workspace_knowledge_base_search(
        {
            "query": "scopedsearchtoken",
            "knowledge_base_ids": [alpha_kb["knowledge_base_id"]],
            "top_k": 8,
        },
        context=context,
    )

    assert response["ok"] is True
    assert response["mode"] == "knowledge_base_search"
    assert response["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert response["scope_applied"]["source_item_ids"] == alpha["source_item_ids"]
    assert {result["source_item_id"] for result in response["results"]} == set(alpha["source_item_ids"])
    assert beta["source_item_ids"][0] not in {result["source_item_id"] for result in response["results"]}
    assert response["results"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert response["results"][0]["knowledge_base_names"] == ["Alpha Search KB"]
    assert response["citations"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert response["retrieval"]["diagnostics"]["score_debug"]["knowledge_base_scope"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert response["knowledge_bases"][0]["knowledge_base_id"] == alpha_kb["knowledge_base_id"]


def test_workspace_ask_empty_knowledge_base_reports_scope_diagnostics() -> None:
    api = _api()
    context = RequestContext()
    empty_kb = api.create_workspace_knowledge_base({"name": "Empty KB"}, context=context)["knowledge_base"]

    response = api.workspace_ask(
        {
            "query": "sharedtoken",
            "intent": "kb_search",
            "skip_intent_classifier": True,
            "scope": {"knowledge_base_ids": [empty_kb["knowledge_base_id"]]},
        },
        context=context,
    )
    diagnostics = response["quality_signals"]["no_answer_diagnostics"]
    by_dimension = {item["dimension"]: item for item in diagnostics["dimensions"]}

    assert response["answer_type"] == "no_answer"
    assert response["route"]["scope_applied"]["knowledge_base_ids"] == [empty_kb["knowledge_base_id"]]
    assert response["route"]["scope_applied"]["source_item_count"] == 0
    readiness = response["route"]["scope_applied"]["knowledge_base_readiness"][0]
    assert readiness["knowledge_base_id"] == empty_kb["knowledge_base_id"]
    assert readiness["retrieval_ready"] is False
    assert readiness["source_item_count"] == 0
    assert response["route"]["scope_applied"]["knowledge_base_readiness_warnings"][0]["status"] == "empty"
    assert diagnostics["primary_reason"] == "selected_knowledge_base_empty"
    assert by_dimension["knowledge_base_scope"]["status"] == "selected_knowledge_base_empty"
    assert by_dimension["knowledge_base_readiness"]["status"] == "empty"
    assert "selected_knowledge_base_empty" in response["quality_signals"]["flags"]
    assert "knowledge_base_empty" in response["quality_signals"]["flags"]


def test_workspace_ask_inaccessible_knowledge_base_scope_does_not_leak_id() -> None:
    api = _api()
    tenant_a = RequestContext(tenant_id="tenant_a", user_id="user_primary")
    tenant_b = RequestContext(tenant_id="tenant_b", user_id="user_primary")
    secret_kb = api.create_workspace_knowledge_base({"name": "Secret tenant A"}, context=tenant_a)["knowledge_base"]

    with pytest.raises(PermissionError) as excinfo:
        api.workspace_ask(
            {
                "query": "sharedtoken",
                "intent": "kb_search",
                "skip_intent_classifier": True,
                "scope": {"knowledge_base_ids": [secret_kb["knowledge_base_id"]]},
            },
            context=tenant_b,
        )

    assert str(excinfo.value) == "knowledge base is not accessible"
    assert secret_kb["knowledge_base_id"] not in str(excinfo.value)


def test_workspace_ask_conversation_persists_knowledge_base_scope() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    alpha = api.create_text_source(
        {
            "title": "Alpha scoped note",
            "text": "persistedscope belongs to alpha ask history.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    api.create_text_source(
        {
            "title": "Beta scoped note",
            "text": "persistedscope belongs to beta ask history.",
            "knowledge_base_id": beta_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )

    created = api.create_workspace_ask_conversation(
        {
            "title": "Scoped conversation",
            "scope": {"mode": "hard", "knowledge_base_ids": [alpha_kb["knowledge_base_id"]]},
        },
        context=context,
    )
    conversation_id = created["conversation"]["conversation_id"]
    events = list(
        api.workspace_ask_conversation_event_stream(
            conversation_id,
            {
                "query": "persistedscope",
                "intent": "kb_search",
                "skip_intent_classifier": True,
                "scope": {"mode": "hard", "knowledge_base_ids": [alpha_kb["knowledge_base_id"]]},
            },
            context=context,
        )
    )
    detail = api.workspace_ask_conversation(conversation_id, context=context)
    user_message = next(message for message in detail["messages"] if message["role"] == "user")
    assistant_message = next(message for message in detail["messages"] if message["role"] == "assistant")
    run = detail["runs"][0]

    assert created["conversation"]["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert created["conversation"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert events[-1][0] == "done"
    assert detail["conversation"]["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert detail["conversation"]["metadata"]["ask_scope"]["source_item_ids"] == alpha["source_item_ids"]
    assert detail["conversation"]["metadata"]["last_query"] == "persistedscope"
    assert user_message["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert assistant_message["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert run["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert run["route"]["scope_applied"]["source_item_ids"] == alpha["source_item_ids"]
    assert {result["source_item_id"] for result in run["result"]["evidence"]["results"]} == set(alpha["source_item_ids"])
    assert run["result"]["evidence"]["results"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert run["result"]["evidence"]["results"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert run["result"]["evidence"]["citations"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert run["result"]["evidence"]["citations"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert run["result"]["evidence"]["source_windows"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert beta_kb["knowledge_base_id"] not in run["result"]["evidence"]["citations"][0]["knowledge_base_ids"]


def test_workspace_corpus_and_documents_filter_by_knowledge_base() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    alpha = api.create_text_source(
        {
            "title": "Alpha note",
            "text": "alpha scoped corpus evidence.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    beta = api.create_text_source(
        {
            "title": "Beta note",
            "text": "beta scoped corpus evidence.",
            "knowledge_base_id": beta_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )

    corpus = api.workspace_corpus(
        knowledge_base_ids=[alpha_kb["knowledge_base_id"]],
        context=context,
    )
    documents = api.workspace_documents_data(
        {"knowledge_base_id": alpha_kb["knowledge_base_id"]},
        context=context,
    )

    assert {item["source_item_id"] for item in corpus["sources"]} == set(alpha["source_item_ids"])
    assert {item["source_item_id"] for item in documents["documents"]} == set(alpha["source_item_ids"])
    assert beta["source_item_ids"][0] not in {item["source_item_id"] for item in documents["documents"]}


def test_graph_digest_and_review_filter_by_knowledge_base_scope() -> None:
    api = _api()
    context = RequestContext()
    alpha_kb = api.create_workspace_knowledge_base({"name": "Alpha KB"}, context=context)["knowledge_base"]
    beta_kb = api.create_workspace_knowledge_base({"name": "Beta KB"}, context=context)["knowledge_base"]
    alpha = api.create_text_source(
        {
            "title": "Alpha note",
            "text": "alpha lineage evidence.",
            "knowledge_base_id": alpha_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    beta = api.create_text_source(
        {
            "title": "Beta note",
            "text": "beta lineage evidence.",
            "knowledge_base_id": beta_kb["knowledge_base_id"],
            "digest_mode": "manual",
        },
        context=context,
    )
    alpha_source_item_id = alpha["source_item_ids"][0]
    beta_source_item_id = beta["source_item_ids"][0]
    alpha_job = api.jobs.submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": context.user_id,
            "source_refs": [{"source_item_id": alpha_source_item_id}],
            "scope": {"source_item_ids": [alpha_source_item_id], "knowledge_base_ids": [alpha_kb["knowledge_base_id"]]},
        },
    )
    beta_job = api.jobs.submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": context.user_id,
            "source_refs": [{"source_item_id": beta_source_item_id}],
            "scope": {"source_item_ids": [beta_source_item_id], "knowledge_base_ids": [beta_kb["knowledge_base_id"]]},
        },
    )
    api.store.add_knowledge_claim(
        KnowledgeClaim(
            knowledge_claim_id="claim_alpha",
            owner_user_id=context.user_id,
            claim_type="fact",
            statement="Alpha claim",
            source_refs=[SourceRef(source_item_id=alpha_source_item_id)],
            evidence_text="alpha lineage evidence",
            job_id=alpha_job.job_id,
            tenant_id=context.tenant_id,
        )
    )
    api.store.add_knowledge_claim(
        KnowledgeClaim(
            knowledge_claim_id="claim_beta",
            owner_user_id=context.user_id,
            claim_type="fact",
            statement="Beta claim",
            source_refs=[SourceRef(source_item_id=beta_source_item_id)],
            evidence_text="beta lineage evidence",
            job_id=beta_job.job_id,
            tenant_id=context.tenant_id,
        )
    )
    api.store.add_digest_note(
        DigestNote(
            digest_note_id="digest_alpha",
            owner_user_id=context.user_id,
            title="Alpha digest",
            synopsis="Alpha synopsis",
            source_refs=[SourceRef(source_item_id=alpha_source_item_id)],
            job_id=alpha_job.job_id,
            tenant_id=context.tenant_id,
        )
    )
    api.store.add_digest_note(
        DigestNote(
            digest_note_id="digest_beta",
            owner_user_id=context.user_id,
            title="Beta digest",
            synopsis="Beta synopsis",
            source_refs=[SourceRef(source_item_id=beta_source_item_id)],
            job_id=beta_job.job_id,
            tenant_id=context.tenant_id,
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="review_alpha",
            owner_user_id=context.user_id,
            review_type=ReviewType.MEMORY_CANDIDATE,
            title="Alpha review",
            proposal={"memory_candidate": "alpha", "source_refs": [{"source_item_id": alpha_source_item_id}], "confidence": 0.8},
            tenant_id=context.tenant_id,
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="review_beta",
            owner_user_id=context.user_id,
            review_type=ReviewType.MEMORY_CANDIDATE,
            title="Beta review",
            proposal={"memory_candidate": "beta", "source_refs": [{"source_item_id": beta_source_item_id}], "confidence": 0.8},
            tenant_id=context.tenant_id,
        )
    )

    graph = api.workspace_graph_data(knowledge_base_ids=[alpha_kb["knowledge_base_id"]], context=context)
    digest_logs = api.digest_logs(
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
        knowledge_base_ids=[alpha_kb["knowledge_base_id"]],
    )
    digest_data = api.workspace_digest_data(
        knowledge_base_ids=[alpha_kb["knowledge_base_id"]],
        context=context,
    )
    reviews = api.console_reviews(knowledge_base_ids=[alpha_kb["knowledge_base_id"]], tenant_id=context.tenant_id)
    digest_run = api.workspace_digest_run(
        {"knowledge_base_id": alpha_kb["knowledge_base_id"], "limit": 10, "run_worker": False, "force": True},
        context=context,
    )

    assert graph["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert {node["object_id"] for node in graph["nodes"] if node.get("object_type") == "source_item"} == {alpha_source_item_id}
    assert {node["object_id"] for node in graph["nodes"] if node.get("object_type") == "knowledge_claim"} == {"claim_alpha"}
    assert {node["object_id"] for node in graph["nodes"] if node.get("object_type") == "digest_note"} == {"digest_alpha"}
    assert {node["object_id"] for node in graph["nodes"] if node.get("object_type") == "review_item"} == {"review_alpha"}
    assert digest_logs["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert [log["job_id"] for log in digest_logs["logs"]] == [alpha_job.job_id]
    assert digest_logs["logs"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert digest_logs["logs"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert digest_logs["logs"][0]["source_refs"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert digest_logs["logs"][0]["knowledge_claims"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert digest_logs["logs"][0]["digest_notes"][0]["source_refs"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert digest_data["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert [claim["knowledge_claim_id"] for claim in digest_data["knowledge_claims"]] == ["claim_alpha"]
    assert digest_data["knowledge_claims"][0]["source_refs"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert digest_data["digest_notes"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert digest_data["review_candidates"][0]["source_refs"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert {item["review_item_id"] for item in reviews["review_items"]} == {"review_alpha"}
    assert reviews["review_items"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert reviews["review_items"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert reviews["review_items"][0]["source_refs"][0]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert reviews["review_items"][0]["source_refs"][0]["knowledge_base_names"] == ["Alpha KB"]
    assert digest_run["scope_applied"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]
    assert set(digest_run["scheduled"]["scheduled_source_item_ids"]) == {alpha_source_item_id}
    assert digest_run["scheduled"]["job"]["payload"]["scope"]["knowledge_base_ids"] == [alpha_kb["knowledge_base_id"]]


def test_writing_board_records_default_knowledge_base_scope() -> None:
    api = _api()
    context = RequestContext()
    created_kb = api.create_workspace_knowledge_base({"name": "Writing KB"}, context=context)["knowledge_base"]

    board = api.workspace_writing_create_board(
        {
            "title": "Writing project",
            "goal": "Draft from scoped evidence.",
            "knowledge_base_id": created_kb["knowledge_base_id"],
            "metadata": {"canvas": "xyflow"},
        },
        context=context,
    )["board"]

    assert board["metadata"]["canvas"] == "xyflow"
    assert board["metadata"]["knowledge_base_ids"] == [created_kb["knowledge_base_id"]]
    assert board["metadata"]["knowledge_base_scope"]["mode"] == "hard"
    assert board["metadata"]["knowledge_base_scope"]["knowledge_base_ids"] == [created_kb["knowledge_base_id"]]


def test_workspace_reader_source_returns_scoped_original_context() -> None:
    api = _api()
    context = RequestContext()
    alpha = api.create_workspace_knowledge_base({"name": "Reader Alpha"}, context=context)["knowledge_base"]
    beta = api.create_workspace_knowledge_base({"name": "Reader Beta"}, context=context)["knowledge_base"]
    item = _source_item("reader_alpha")
    item.content_text = "Alpha reader source content."
    api.store.upsert_source_item(item)
    api.store.add_document(
        Document(
            document_id="reader_doc",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            title="Reader document",
            body="Heading\n\nAlpha paragraph one.\n\nAlpha paragraph two.",
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_chunk(
        Chunk(
            chunk_id="reader_chunk_1",
            document_id="reader_doc",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            text="Alpha paragraph one.",
            ordinal=1,
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=alpha["knowledge_base_id"],
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            added_by_user_id=item.owner_user_id,
            tenant_id=item.tenant_id,
        )
    )

    reader = api.workspace_reader_source(
        {"source_item_id": item.source_item_id, "knowledge_base_id": alpha["knowledge_base_id"]},
        context=context,
    )

    assert reader["source_item"]["source_item_id"] == item.source_item_id
    assert reader["source_item"]["knowledge_base_ids"] == [alpha["knowledge_base_id"]]
    assert reader["scope_applied"]["knowledge_base_ids"] == [alpha["knowledge_base_id"]]
    assert reader["documents"][0]["body"] == "Heading\n\nAlpha paragraph one.\n\nAlpha paragraph two."
    assert reader["chunks"][0]["chunk_id"] == "reader_chunk_1"
    assert reader["passage_windows"][0]["text"].startswith("Heading")
    assert reader["counts"] == {"documents": 1, "chunks": 1, "passage_windows": 1}

    with pytest.raises(PermissionError):
        api.workspace_reader_source(
            {"source_item_id": item.source_item_id, "knowledge_base_id": beta["knowledge_base_id"]},
            context=context,
        )


def test_workspace_reader_source_enforces_tenant_and_owner_acl() -> None:
    api = _api()
    context = RequestContext(tenant_id=TENANT_A, user_id="user_a", represented_user_id="user_a")
    other_context = RequestContext(tenant_id=TENANT_B, user_id="user_b", represented_user_id="user_b")
    kb = api.create_workspace_knowledge_base({"name": "Tenant Reader"}, context=context)["knowledge_base"]
    item = _source_item("tenant_reader", tenant_id=TENANT_A, owner_user_id="user_a")
    api.store.upsert_source_item(item)
    api.store.add_document(
        Document(
            document_id="tenant_reader_doc",
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=[],
            title="Tenant reader document",
            body="Tenant A only.",
            tenant_id=item.tenant_id,
        )
    )
    api.store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=kb["knowledge_base_id"],
            source_item_id=item.source_item_id,
            owner_user_id=item.owner_user_id,
            added_by_user_id=item.owner_user_id,
            tenant_id=item.tenant_id,
        )
    )

    assert api.workspace_reader_source({"source_item_id": item.source_item_id}, context=context)["documents"][0]["body"] == "Tenant A only."
    with pytest.raises(PermissionError):
        api.workspace_reader_source({"source_item_id": item.source_item_id}, context=other_context)


def _api() -> PSKAApi:
    api = object.__new__(PSKAApi)
    api.config = PSKAConfig(service=ServiceConfig(), auth=AuthConfig())
    api.store = InMemoryKnowledgeStore()
    api.store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    api.retrieval = RetrievalService(api.store, ACLService(api.store))
    api.jobs = JobService(api.store)
    api.agentic_service = object()
    return api


def _source_item(
    source_item_id: str,
    *,
    tenant_id: str = "tenant_default",
    owner_user_id: str = "user_primary",
) -> SourceItem:
    return SourceItem(
        source_item_id=source_item_id,
        source_channel="manual",
        record_type="note",
        source_id=source_item_id,
        owner_user_id=owner_user_id,
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        visible_team_ids=[],
        title=f"Source {source_item_id}",
        url=None,
        content_text=f"Content for {source_item_id}.",
        content_hash=f"hash_{tenant_id}_{owner_user_id}_{source_item_id}",
        tenant_id=tenant_id,
    )
