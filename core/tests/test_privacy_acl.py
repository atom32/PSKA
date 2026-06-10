from __future__ import annotations

from pathlib import Path

from pska_core.acl import ACLService
from pska_core.enums import UserRole, Visibility
from pska_core.ingest import IngestService
from pska_core.models import TeamMembership, User
from pska_core.retrieval import RetrievalService
from pska_core.store import InMemoryKnowledgeStore


def payload(**overrides):
    data = {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "note",
        "source_id": "note-1",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "visible_team_ids": [],
        "title": "Private note",
        "content": {"text": "blue notebook contains the exact travel plan"},
    }
    data.update(overrides)
    return data


def make_store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("user_secondary", "secondary", UserRole.USER))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    store.add_team_membership(TeamMembership("user_primary", "team_default"))
    store.add_team_membership(TeamMembership("user_secondary", "team_default"))
    return store


def test_example_config_uses_only_anonymous_ids() -> None:
    text = Path("config.example.toml").read_text(encoding="utf-8").lower()
    assert "user_primary" in text
    assert "team_shared_default" in text
    assert "/users/" not in text
    assert "sk-" not in text


def test_private_data_visible_only_to_owner_or_admin() -> None:
    store = make_store()
    item = IngestService(store).ingest_channel_payload(payload())
    acl = ACLService(store)

    assert acl.can_read_item(store.get_user("user_primary"), item)
    assert not acl.can_read_item(store.get_user("user_secondary"), item)


def test_team_visible_data_visible_to_selected_team_members() -> None:
    store = make_store()
    item = IngestService(store).ingest_channel_payload(
        payload(
            source_id="note-team",
            visibility="team",
            visible_team_ids=["team_default"],
            space_id="team_shared_default",
            content={"text": "shared insurance renewal date"},
        )
    )
    acl = ACLService(store)

    assert acl.can_read_item(store.get_user("user_secondary"), item)


def test_agent_service_cannot_bypass_acl_without_represented_user() -> None:
    store = make_store()
    item = IngestService(store).ingest_channel_payload(payload())
    acl = ACLService(store)

    assert not acl.can_read_item(store.get_user("agent_service"), item)
    assert acl.can_read_item(store.get_user("agent_service"), item, represented_user_id="user_primary")
    assert not acl.can_read_item(store.get_user("agent_service"), item, represented_user_id="user_secondary")


def test_acl_filter_happens_before_retrieval_ranking() -> None:
    store = make_store()
    ingest = IngestService(store)
    ingest.ingest_channel_payload(payload(content={"text": "secret rarekeyword"}))
    ingest.ingest_channel_payload(
        payload(
            source_id="shared-1",
            title="shared rarekeyword",
            visibility="team",
            visible_team_ids=["team_default"],
            space_id="team_shared_default",
            content={"text": "shared rarekeyword"},
        )
    )
    response = RetrievalService(store, ACLService(store)).search("rarekeyword", store.get_user("user_secondary"))

    assert [result.title for result in response.results] == ["shared rarekeyword"]


def test_vector_search_recalls_semantic_match_without_lexical_overlap() -> None:
    store = make_store()
    provider = FakeEmbeddingProvider()
    ingest = IngestService(store, embedding_provider=provider)
    ingest.ingest_channel_payload(payload(source_id="alpha", title="Alpha note", content={"text": "alpha document"}))
    ingest.ingest_channel_payload(payload(source_id="beta", title="Beta note", content={"text": "beta document"}))

    response = RetrievalService(store, ACLService(store), embedding_provider=provider).search(
        "semantic intent",
        store.get_user("user_primary"),
    )

    assert response.results[0].title == "Alpha note"
    assert response.results[0].score_debug["vector"] > 0.99
    assert response.score_debug["vector_enabled"] is True


class FakeEmbeddingProvider:
    provider_name = "fake"
    model_name = "fake-semantic"
    dimensions = 2

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if "alpha" in text or "semantic" in text:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors
