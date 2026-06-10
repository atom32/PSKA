from __future__ import annotations

from pska_core.enums import UserRole, Visibility
from pska_core.models import SourceItem, User
from pska_core.store import KnowledgeStore


class ACLService:
    """Private-first access control for PSKA knowledge objects."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def visible_team_ids_for_user(self, user_id: str) -> set[str]:
        return {membership.team_id for membership in self.store.team_memberships_for_user(user_id)}

    def can_read_item(self, user: User, item: SourceItem, *, represented_user_id: str | None = None) -> bool:
        if user.status != "active":
            return False
        if user.role == UserRole.ADMIN:
            return True
        if user.role == UserRole.AGENT_SERVICE:
            return bool(represented_user_id) and self.can_read_item(
                self.store.get_user(represented_user_id),
                item,
            )
        if item.visibility == Visibility.PUBLIC:
            return True
        if item.owner_user_id == user.user_id:
            return True
        if item.visibility == Visibility.TEAM:
            user_teams = self.visible_team_ids_for_user(user.user_id)
            return bool(user_teams.intersection(item.visible_team_ids))
        return False

    def filter_visible_items(
        self,
        user: User,
        items: list[SourceItem],
        *,
        represented_user_id: str | None = None,
    ) -> list[SourceItem]:
        return [item for item in items if self.can_read_item(user, item, represented_user_id=represented_user_id)]
