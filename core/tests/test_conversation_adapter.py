from __future__ import annotations

from pska_core.adapters.conversation import conversation_to_payload
from pska_core.acl import ACLService
from pska_core.enums import UserRole
from pska_core.ingest import IngestService
from pska_core.models import User
from pska_core.retrieval import RetrievalService
from pska_core.store import InMemoryKnowledgeStore


def test_conversation_payload_ingests_as_source_document_and_chunk() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    payload = conversation_to_payload(
        {
            "id": "conv_1",
            "source_channel": "fastreact",
            "participants": [{"participant_id": "user_primary", "name": "Dawei"}],
            "messages": [
                {
                    "id": "msg_1",
                    "role": "user",
                    "participant_id": "user_primary",
                    "participant_name": "Dawei",
                    "content": "Remember that Project Atlas uses PSKA.",
                    "created_at": "2026-06-11T10:00:00Z",
                },
                {
                    "id": "msg_2",
                    "role": "assistant",
                    "content": "Noted. I will keep that as a project fact.",
                    "created_at": "2026-06-11T10:00:05Z",
                    "citations": [{"source_item_id": "src_existing"}],
                },
            ],
            "tool_calls": [{"tool_name": "pska_search", "arguments": {"query": "Project Atlas"}}],
        },
        owner_user_id="user_primary",
        space_id="private_primary",
    )

    source = IngestService(store).ingest_channel_payload(payload)

    assert source.source_channel == "fastreact"
    assert source.record_type == "conversation"
    assert source.source_id == "conv_1"
    assert source.metadata["extra"]["message_count"] == 2
    assert source.metadata["author"]["participants"][0]["participant_id"] == "user_primary"
    assert "msg_1 Dawei [2026-06-11T10:00:00Z]" in source.content_text
    assert "Project Atlas uses PSKA" in next(iter(store.chunks.values())).text


def test_conversation_payload_requires_non_empty_messages() -> None:
    try:
        conversation_to_payload({"id": "conv_empty", "messages": []}, owner_user_id="user_primary", space_id="private")
    except ValueError as exc:
        assert "messages" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_conversation_search_citation_includes_message_id() -> None:
    store = InMemoryKnowledgeStore()
    user = User("user_primary", "primary", UserRole.ADMIN)
    store.add_user(user)
    payload = conversation_to_payload(
        {
            "id": "conv_search",
            "messages": [
                {"id": "msg_pref", "role": "user", "content": "I prefer concise project updates."},
                {"id": "msg_noise", "role": "assistant", "content": "Understood."},
            ],
        },
        owner_user_id="user_primary",
        space_id="private_primary",
    )
    IngestService(store).ingest_channel_payload(payload)

    response = RetrievalService(store, ACLService(store)).search("concise project updates", user)

    assert response.results
    assert "msg_pref" in response.results[0].citation["message_ids"]
