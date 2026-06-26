from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
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
    subject: str = ""
    display_name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    auth_provider: str = "service_token"

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


def context_from_headers(
    headers: Mapping[str, str],
    payload: dict[str, Any] | None = None,
    *,
    service_authenticated: bool = False,
    auth_config: Any | None = None,
) -> RequestContext:
    mode = _auth_mode(auth_config)
    if mode == "trusted_headers":
        return context_from_trusted_headers(headers, payload, service_authenticated=service_authenticated, auth_config=auth_config)
    if mode == "jwt":
        return context_from_jwt(headers, payload, service_authenticated=service_authenticated, auth_config=auth_config)
    if mode != "service_token":
        raise AuthError(f"unsupported PSKA auth mode: {mode}")
    return context_from_service_token(headers, payload, service_authenticated=service_authenticated, auth_provider="service_token")


def context_from_service_token(
    headers: Mapping[str, str],
    payload: dict[str, Any] | None = None,
    *,
    service_authenticated: bool = False,
    auth_provider: str = "service_token",
) -> RequestContext:
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
        subject=str(payload.get("subject") or user_id),
        groups=_list_claim(payload.get("groups")),
        roles=_list_claim(payload.get("roles")),
        auth_provider=auth_provider,
    )


def context_from_trusted_headers(
    headers: Mapping[str, str],
    payload: dict[str, Any] | None = None,
    *,
    service_authenticated: bool = False,
    auth_config: Any | None = None,
) -> RequestContext:
    payload = payload or {}
    user_id = _trusted_header(headers, auth_config, "trusted_header_user_id", "X-PSKA-User-Id", "X-FastReAct-User-Key")
    if not user_id:
        raise AuthError("PSKA trusted identity header required")
    tenant_id = _trusted_header(headers, auth_config, "trusted_header_tenant_id", "X-PSKA-Tenant-Id", "X-FastReAct-Tenant-Key")
    represented_user_id = _trusted_header(headers, auth_config, "trusted_header_represented_user_id", "X-PSKA-Represented-User-Id")
    subject = _trusted_header(headers, auth_config, "trusted_header_subject", "X-PSKA-Subject", "X-FastReAct-Subject") or user_id
    caller = str(headers.get("X-PSKA-Caller") or payload.get("caller") or "user")
    return RequestContext(
        tenant_id=str(tenant_id or payload.get("tenant_id") or _tenant_from_user_key(user_id) or DEFAULT_TENANT_ID),
        user_id=str(_pska_user_id_from_key(user_id)),
        represented_user_id=str(represented_user_id) if represented_user_id else None,
        caller=caller,
        service_authenticated=service_authenticated,
        scope=_scope_from(headers.get("X-PSKA-Scope"), payload.get("scope")),
        subject=str(subject),
        display_name=_trusted_header(headers, auth_config, "trusted_header_display_name", "X-PSKA-Display-Name", "X-FastReAct-Display-Name"),
        email=_trusted_header(headers, auth_config, "trusted_header_email", "X-PSKA-Email", "X-FastReAct-Email"),
        groups=_list_claim(_trusted_header(headers, auth_config, "trusted_header_groups", "X-PSKA-Groups", "X-FastReAct-Groups")),
        roles=_list_claim(_trusted_header(headers, auth_config, "trusted_header_roles", "X-PSKA-Roles", "X-FastReAct-Roles")),
        auth_provider=_trusted_header(headers, auth_config, "trusted_header_provider", "X-PSKA-Auth-Provider", "X-FastReAct-Auth-Provider") or "trusted_headers",
    )


def context_from_jwt(
    headers: Mapping[str, str],
    payload: dict[str, Any] | None = None,
    *,
    service_authenticated: bool = False,
    auth_config: Any | None = None,
) -> RequestContext:
    claims = verify_hs256_jwt(
        _required_bearer_token(headers),
        str(getattr(auth_config, "jwt_secret", "") or ""),
        issuer=getattr(auth_config, "jwt_issuer", None),
        audience=getattr(auth_config, "jwt_audience", None),
    )
    user_claim = str(getattr(auth_config, "jwt_user_claim", "sub") or "sub")
    subject = str(claims.get("sub") or claims.get("user_key") or claims.get(user_claim) or "").strip()
    if not subject:
        raise AuthError("JWT subject claim required")
    tenant_id = ""
    for claim_name in getattr(auth_config, "jwt_tenant_claims", []) or ["tenant_id", "tenant_key", "tenant", "org_id"]:
        value = claims.get(claim_name)
        if value:
            tenant_id = str(value).strip()
            break
    user_identity = str(claims.get("user_id") or claims.get(user_claim) or claims.get("user_key") or subject).strip()
    represented_claim = str(getattr(auth_config, "jwt_represented_user_claim", "represented_user_id") or "represented_user_id")
    provider_claim = str(getattr(auth_config, "jwt_provider_claim", "provider") or "provider")
    return RequestContext(
        tenant_id=tenant_id or _tenant_from_user_key(user_identity) or DEFAULT_TENANT_ID,
        user_id=_pska_user_id_from_key(user_identity),
        represented_user_id=str(claims.get(represented_claim)) if claims.get(represented_claim) else None,
        caller="user",
        service_authenticated=service_authenticated,
        scope=_scope_from(headers.get("X-PSKA-Scope"), (payload or {}).get("scope") if payload else None),
        subject=subject,
        display_name=str(claims.get(getattr(auth_config, "jwt_display_name_claim", "name")) or ""),
        email=str(claims.get(getattr(auth_config, "jwt_email_claim", "email")) or ""),
        groups=_list_claim(claims.get(getattr(auth_config, "jwt_groups_claim", "groups"))),
        roles=_list_claim(claims.get(getattr(auth_config, "jwt_roles_claim", "roles"))),
        auth_provider=str(claims.get(provider_claim) or "jwt"),
    )


def verify_hs256_jwt(token: str, secret: str, *, issuer: str | None = None, audience: str | None = None) -> dict[str, Any]:
    if not secret:
        raise AuthError("JWT auth requires jwt_secret")
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("JWT must have three segments")
    header_segment, payload_segment, signature_segment = parts
    header = _decode_json_segment(header_segment)
    if header.get("alg") != "HS256":
        raise AuthError("Only HS256 JWTs are supported")
    signing_input = f"{header_segment}.{payload_segment}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    provided = _decode_segment(signature_segment)
    if not hmac.compare_digest(expected, provided):
        raise AuthError("JWT signature is invalid")
    claims = _decode_json_segment(payload_segment)
    now = datetime.now(timezone.utc).timestamp()
    if "exp" in claims and now >= float(claims["exp"]):
        raise AuthError("JWT has expired")
    if "nbf" in claims and now < float(claims["nbf"]):
        raise AuthError("JWT is not valid yet")
    if issuer and claims.get("iss") != issuer:
        raise AuthError("JWT issuer is invalid")
    if audience:
        audiences = _list_claim(claims.get("aud"))
        if audience not in audiences:
            raise AuthError("JWT audience is invalid")
    return claims


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    return value[len(prefix) :] if value.startswith(prefix) else None


def _required_bearer_token(headers: Mapping[str, str]) -> str:
    token = _bearer_token(headers.get("Authorization") or headers.get("authorization"))
    if not token:
        raise AuthError("Bearer JWT required")
    return token


def _auth_mode(auth_config: Any | None) -> str:
    return str(getattr(auth_config, "mode", "service_token") or "service_token").strip().lower()


def _trusted_header(headers: Mapping[str, str], auth_config: Any | None, attr: str, default: str, alias: str | None = None) -> str:
    configured = str(getattr(auth_config, attr, default) or default)
    for name in [configured, default, alias]:
        if name:
            value = headers.get(name)
            if value:
                return value.strip()
    return ""


def _tenant_from_user_key(user_id: str) -> str:
    return user_id.split(":", 1)[0] if ":" in user_id else ""


def _pska_user_id_from_key(user_id: str) -> str:
    if user_id.startswith("pska:"):
        return user_id.split(":", 1)[1]
    return user_id


def _list_claim(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value)]


def _decode_json_segment(segment: str) -> dict[str, Any]:
    try:
        payload = json.loads(_decode_segment(segment).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AuthError("JWT segment is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthError("JWT segment must decode to an object")
    return payload


def _decode_segment(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except ValueError as exc:
        raise AuthError("JWT segment is not valid base64url") from exc


def _scope_from(header_value: str | None, payload_value: Any) -> dict[str, Any]:
    if header_value:
        try:
            parsed = json.loads(header_value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(payload_value) if isinstance(payload_value, dict) else {}
