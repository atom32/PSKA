from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"
    VIEWER = "viewer"
    AGENT_SERVICE = "agent_service"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Visibility(StrEnum):
    PRIVATE = "private"
    TEAM = "team"
    PUBLIC = "public"
    SYSTEM = "system"


class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROFILE = "profile"


class ReviewType(StrEnum):
    SHARE_PROPOSAL = "share_proposal"
    SENSITIVE_CONTENT = "sensitive_content"
    PROFILE_UPDATE = "profile_update"
    ENTITY_MERGE = "entity_merge"
    CONFLICT = "conflict"
    MEMORY_CANDIDATE = "memory_candidate"
    RELATIONSHIP_CANDIDATE = "relationship_candidate"
    ACTION_CANDIDATE = "action_candidate"
    LOW_CONFIDENCE = "low_confidence"


class Directionality(StrEnum):
    DIRECTED = "directed"
    UNDIRECTED = "undirected"
    AMBIGUOUS = "ambiguous"
