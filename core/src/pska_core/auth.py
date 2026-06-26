from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import json
from typing import Any, Mapping

from pska_core.models import DEFAULT_TENANT_ID


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RequestContext:
    tenant_id: str = DEFAULT_TENANT_ID
    user_id: str = "user_primary"
    represented_user_id: str | None = None
    caller: str = "user"
    service_authenticated: bool = False
    scope: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_user_id(self) -> str:
        if self.caller == "agent_service":
            return "agent_service"
        return self.user_id

    def apply_to_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        merged = dict(payload)
        merged["tenant_id"] = self.tenant_id
        if self.caller == "agent_service":
            merged["user_id"] = "agent_service"
            if self.represented_user_id and not merged.get("represented_user_id"):
                merged["represented_user_id"] = self.represented_user_id
            return merged
        merged.setdefault("user_id", self.user_id)
        if self.represented_user_id and not merged.get("represented_user_id"):
            merged["represented_user_id"] = self.represented_user_id
        return merged


def service_token_required(service_token: str | None = None) -> bool:
    return bool(service_token)


def authenticate_headers(headers: Mapping[str, str], service_token: str | None = None) -> bool:
    expected = service_token
    if not expected:
        return False
    provided = headers.get("X-PSKA-Service-Token") or _bearer_token(headers.get("Authorization"))
    if not provided or not hmac.compare_digest(provided, expected):
        raise AuthError("PSKA service token required")
    return True


def context_from_headers(headers: Mapping[str, str], payload: dict[str, Any] | None = None, *, service_authenticated: bool = False) -> RequestContext:
    payload = payload or {}
    tenant_id = str(headers.get("X-PSKA-Tenant-Id") or payload.get("tenant_id") or DEFAULT_TENANT_ID)
    caller = str(headers.get("X-PSKA-Caller") or payload.get("caller") or "user")
    user_id = str(headers.get("X-PSKA-User-Id") or payload.get("user_id") or ("agent_service" if caller == "agent_service" else "user_primary"))
    represented_user_id = headers.get("X-PSKA-Represented-User-Id") or payload.get("represented_user_id")
    scope = _scope_from(headers.get("X-PSKA-Scope"), payload.get("scope"))
    return RequestContext(
        tenant_id=tenant_id,
        user_id=user_id,
        represented_user_id=str(represented_user_id) if represented_user_id else None,
        caller=caller,
        service_authenticated=service_authenticated,
        scope=scope,
    )


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    return value[len(prefix) :] if value.startswith(prefix) else None


def _scope_from(header_value: str | None, payload_value: Any) -> dict[str, Any]:
    if header_value:
        try:
            parsed = json.loads(header_value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(payload_value) if isinstance(payload_value, dict) else {}
