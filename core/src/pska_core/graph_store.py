from __future__ import annotations

from typing import Protocol

from pska_core.models import Entity, Hyperedge, HyperedgeMember
from pska_core.store import KnowledgeStore


class GraphStore(Protocol):
    def list_entities(self, *, tenant_id: str | None = None) -> list[Entity]: ...

    def neighbors(self, entity_ids: set[str], *, depth: int = 1) -> list[tuple[Hyperedge, list[HyperedgeMember]]]: ...


class PostgresGraphStore:
    """Graph adapter over the PSKA Postgres-first graph tables."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def list_entities(self, *, tenant_id: str | None = None) -> list[Entity]:
        return self.store.list_entities(tenant_id=tenant_id)

    def neighbors(self, entity_ids: set[str], *, depth: int = 1) -> list[tuple[Hyperedge, list[HyperedgeMember]]]:
        if depth < 1:
            return []
        seen_edges: set[str] = set()
        frontier = set(entity_ids)
        results: list[tuple[Hyperedge, list[HyperedgeMember]]] = []
        for _ in range(depth):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for edge, members in self.store.list_hyperedges_for_entities(frontier):
                if edge.hyperedge_id in seen_edges:
                    continue
                seen_edges.add(edge.hyperedge_id)
                results.append((edge, members))
                next_frontier.update(member.entity_id for member in members)
            frontier = next_frontier - frontier
        return results
