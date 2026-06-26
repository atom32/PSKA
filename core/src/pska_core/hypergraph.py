from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from pska_core.enums import Directionality
from pska_core.models import DEFAULT_TENANT_ID, Entity, Hyperedge, HyperedgeMember, SourceRef
from pska_core.store import KnowledgeStore


class HypergraphService:
    """Creates entity sets and role-labeled hyperedges."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def create_entity(self, entity: Entity) -> Entity:
        self.store.add_entity(entity)
        return entity

    def create_hyperedge(
        self,
        *,
        relation_type: str,
        owner_user_id: str,
        space_id: str,
        visibility,
        members: list[tuple[str, str]],
        visible_team_ids: list[str] | None = None,
        directionality: Directionality = Directionality.AMBIGUOUS,
        evidence_text: str = "",
        source_refs: list[SourceRef] | None = None,
        confidence: float = 0.0,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Hyperedge:
        if len(members) < 2:
            raise ValueError("hyperedge requires at least two members")
        edge_id_parts = [
            relation_type,
            owner_user_id,
            space_id,
            evidence_text,
            *[f"{entity_id}:{role}" for entity_id, role in members],
        ]
        if tenant_id != DEFAULT_TENANT_ID:
            edge_id_parts.insert(0, tenant_id)
        edge_id_basis = "|".join(edge_id_parts)
        hyperedge = Hyperedge(
            hyperedge_id=f"hed_{uuid5(NAMESPACE_URL, edge_id_basis).hex}",
            relation_type=relation_type,
            owner_user_id=owner_user_id,
            space_id=space_id,
            visibility=visibility,
            directionality=directionality,
            visible_team_ids=visible_team_ids or [],
            evidence_text=evidence_text,
            source_refs=source_refs or [],
            confidence=confidence,
            tenant_id=tenant_id,
        )
        edge_members = [
            HyperedgeMember(hyperedge_id=hyperedge.hyperedge_id, entity_id=entity_id, role=role, ordinal=index)
            for index, (entity_id, role) in enumerate(members)
        ]
        self.store.add_hyperedge(hyperedge, edge_members)
        return hyperedge
