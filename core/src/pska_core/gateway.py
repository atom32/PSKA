from __future__ import annotations

from dataclasses import dataclass
import base64
from collections.abc import Mapping
import hashlib
import hmac
import html
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

from pska_core.auth import AuthError, verify_hs256_jwt


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
DEFAULT_SESSION_COOKIE = "pska_gateway_session"
DEFAULT_PSKA_AUDIENCE = "pska"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
    "cookie",
    "set-cookie",
}
IDENTITY_HEADER_PREFIXES = ("x-pska-", "x-fastreact-", "x-authnode-")
IDENTITY_HEADERS = {"authorization"}


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    frontend_dist: Path = DEFAULT_FRONTEND_DIST
    pska_url: str = "http://127.0.0.1:8765"
    authnode_url: str = "http://127.0.0.1:8788"
    authnode_browser_url: str | None = None
    authnode_admin_token: str | None = None
    pska_service_token: str | None = None
    session_secret: str | None = None
    cookie_name: str = DEFAULT_SESSION_COOKIE
    cookie_secure: bool = False
    token_ttl_seconds: int = 3600
    request_timeout_seconds: float = 15.0
    default_tenant_id: str = "tenant_default"
    default_user_key: str = "pska:user_primary"
    authnode_browser_login: bool = True
    authnode_logout: bool = True
    local_authnode_catalog_login: bool = True
    callback_jwt_secret: str | None = None
    callback_jwt_issuer: str | None = None
    callback_jwt_audience: str | None = DEFAULT_PSKA_AUDIENCE

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        secret = os.getenv("PSKA_GATEWAY_SESSION_SECRET")
        return cls(
            host=os.getenv("PSKA_GATEWAY_HOST", "127.0.0.1"),
            port=_int_env("PSKA_GATEWAY_PORT", 8080),
            frontend_dist=Path(os.getenv("PSKA_GATEWAY_FRONTEND_DIST", str(DEFAULT_FRONTEND_DIST))).expanduser(),
            pska_url=os.getenv("PSKA_GATEWAY_PSKA_URL", "http://127.0.0.1:8765").rstrip("/"),
            authnode_url=(os.getenv("PSKA_GATEWAY_AUTHNODE_URL") or os.getenv("AUTHNODE_URL") or "http://127.0.0.1:8788").rstrip("/"),
            authnode_browser_url=_optional_url_env("PSKA_GATEWAY_AUTHNODE_BROWSER_URL") or _optional_url_env("AUTHNODE_BROWSER_URL"),
            authnode_admin_token=os.getenv("PSKA_GATEWAY_AUTHNODE_ADMIN_TOKEN") or os.getenv("AUTHNODE_ADMIN_TOKEN") or None,
            pska_service_token=os.getenv("PSKA_GATEWAY_PSKA_SERVICE_TOKEN") or None,
            session_secret=secret,
            cookie_name=os.getenv("PSKA_GATEWAY_COOKIE_NAME", DEFAULT_SESSION_COOKIE),
            cookie_secure=_bool_env("PSKA_GATEWAY_COOKIE_SECURE", False),
            token_ttl_seconds=_int_env("PSKA_GATEWAY_TOKEN_TTL_SECONDS", 3600),
            request_timeout_seconds=float(os.getenv("PSKA_GATEWAY_REQUEST_TIMEOUT_SECONDS", "15")),
            default_tenant_id=os.getenv("PSKA_GATEWAY_DEFAULT_TENANT_ID", "tenant_default"),
            default_user_key=os.getenv("PSKA_GATEWAY_DEFAULT_USER_KEY", "pska:user_primary"),
            authnode_browser_login=_bool_env("PSKA_GATEWAY_AUTHNODE_BROWSER_LOGIN", True),
            authnode_logout=_bool_env("PSKA_GATEWAY_AUTHNODE_LOGOUT", True),
            local_authnode_catalog_login=_bool_env("PSKA_GATEWAY_LOCAL_AUTHNODE_CATALOG_LOGIN", True),
            callback_jwt_secret=os.getenv("PSKA_GATEWAY_AUTH_JWT_SECRET")
            or os.getenv("PSKA_AUTH_JWT_SECRET")
            or os.getenv("AUTHNODE_JWT_SECRET")
            or None,
            callback_jwt_issuer=os.getenv("PSKA_GATEWAY_AUTH_JWT_ISSUER") or os.getenv("PSKA_AUTH_JWT_ISSUER") or None,
            callback_jwt_audience=os.getenv("PSKA_GATEWAY_AUTH_JWT_AUDIENCE") or os.getenv("PSKA_AUTH_JWT_AUDIENCE") or DEFAULT_PSKA_AUDIENCE,
        )

    def with_runtime_defaults(self) -> "GatewayConfig":
        if self.session_secret:
            return self
        return GatewayConfig(
            host=self.host,
            port=self.port,
            frontend_dist=self.frontend_dist,
            pska_url=self.pska_url,
            authnode_url=self.authnode_url,
            authnode_browser_url=self.authnode_browser_url,
            authnode_admin_token=self.authnode_admin_token,
            pska_service_token=self.pska_service_token,
            session_secret=secrets.token_urlsafe(48),
            cookie_name=self.cookie_name,
            cookie_secure=self.cookie_secure,
            token_ttl_seconds=self.token_ttl_seconds,
            request_timeout_seconds=self.request_timeout_seconds,
            default_tenant_id=self.default_tenant_id,
            default_user_key=self.default_user_key,
            authnode_browser_login=self.authnode_browser_login,
            authnode_logout=self.authnode_logout,
            local_authnode_catalog_login=self.local_authnode_catalog_login,
            callback_jwt_secret=self.callback_jwt_secret,
            callback_jwt_issuer=self.callback_jwt_issuer,
            callback_jwt_audience=self.callback_jwt_audience,
        )


class GatewayError(RuntimeError):
    pass


def serve_gateway(config: GatewayConfig | None = None) -> None:
    base_config = config or GatewayConfig.from_env()
    secret_configured = bool(base_config.session_secret)
    runtime_config = base_config.with_runtime_defaults()
    if not secret_configured:
        print("PSKA Gateway using an ephemeral session secret; set PSKA_GATEWAY_SESSION_SECRET for stable sessions.")

    class Handler(PSKAGatewayHandler):
        pass

    Handler.config = runtime_config
    server = ThreadingHTTPServer((runtime_config.host, runtime_config.port), Handler)
    print(
        "PSKA Gateway listening on "
        f"http://{runtime_config.host}:{runtime_config.port} "
        f"(frontend={runtime_config.frontend_dist}, pska={runtime_config.pska_url}, authnode={runtime_config.authnode_url})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def encode_session(session: Mapping[str, Any], secret: str) -> str:
    payload = json.dumps(dict(session), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_segment = _b64url_encode(payload)
    signature = hmac.new(secret.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_segment}.{_b64url_encode(signature)}"


def decode_session(value: str | None, secret: str, *, now: float | None = None) -> dict[str, Any] | None:
    if not value or not secret or "." not in value:
        return None
    payload_segment, signature_segment = value.split(".", 1)
    expected = _b64url_encode(hmac.new(secret.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature_segment):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = _int_value(payload.get("exp"))
    if exp is not None and (now if now is not None else time.time()) >= exp:
        return None
    return payload


def request_authnode_token(
    config: GatewayConfig,
    *,
    user_key: str,
    tenant_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_key": user_key,
        "tenant_id": tenant_id,
        "audience": DEFAULT_PSKA_AUDIENCE,
        "ttl_seconds": int(config.token_ttl_seconds),
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.authnode_admin_token:
        headers["X-AuthNode-Admin-Token"] = config.authnode_admin_token
    request = Request(f"{config.authnode_url.rstrip('/')}/v1/token", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=config.request_timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"AuthNode token request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise GatewayError(f"AuthNode token request unavailable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GatewayError("AuthNode token request timed out") from exc
    except json.JSONDecodeError as exc:
        raise GatewayError("AuthNode token request returned invalid JSON") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise GatewayError("AuthNode token request returned no access_token")
    return data


def request_authnode_public_user(
    config: GatewayConfig,
    *,
    user_key: str,
    tenant_id: str,
) -> dict[str, Any]:
    request = Request(f"{config.authnode_url.rstrip('/')}/v1/users", headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=config.request_timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"AuthNode users request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise GatewayError(f"AuthNode users request unavailable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GatewayError("AuthNode users request timed out") from exc
    except json.JSONDecodeError as exc:
        raise GatewayError("AuthNode users request returned invalid JSON") from exc
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list):
        raise GatewayError("AuthNode users request returned no users list")
    requested_user = user_key.strip()
    requested_tenant = tenant_id.strip()
    for item in users:
        if not isinstance(item, dict):
            continue
        item_user_key = str(item.get("user_key") or "").strip()
        item_user_id = str(item.get("user_id") or "").strip()
        item_tenant_id = str(item.get("tenant_id") or "").strip()
        item_tenant_key = str(item.get("tenant_key") or "").strip()
        user_matches = requested_user in {item_user_key, item_user_id}
        tenant_matches = requested_tenant in {item_tenant_id, item_tenant_key}
        if user_matches and tenant_matches:
            return item
    raise GatewayError(f"AuthNode user not found for tenant={tenant_id!r} user={user_key!r}")


def request_authnode_code_exchange(config: GatewayConfig, *, code: str, target: str = DEFAULT_PSKA_AUDIENCE) -> dict[str, Any]:
    payload = {"code": code, "target": target}
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{config.authnode_url.rstrip('/')}/v1/auth/exchange",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.request_timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"AuthNode code exchange failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise GatewayError(f"AuthNode code exchange unavailable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GatewayError("AuthNode code exchange timed out") from exc
    except json.JSONDecodeError as exc:
        raise GatewayError("AuthNode code exchange returned invalid JSON") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise GatewayError("AuthNode code exchange returned no access_token")
    return data


def session_from_token_response(data: Mapping[str, Any], *, requested_user_key: str, requested_tenant_id: str) -> dict[str, Any]:
    claims = data.get("claims") if isinstance(data.get("claims"), dict) else {}
    exp = _int_value(claims.get("exp")) or int(time.time()) + 3600
    user_id = _normalized_user_id(claims, fallback=requested_user_key)
    tenant_id = _normalized_tenant_id(claims, fallback=requested_tenant_id)
    subject = str(claims.get("sub") or claims.get("user_key") or requested_user_key)
    return {
        "token": str(data["access_token"]),
        "exp": exp,
        "iat": _int_value(claims.get("iat")) or int(time.time()),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "represented_user_id": user_id,
        "subject": subject,
        "display_name": str(claims.get("name") or ""),
        "email": str(claims.get("email") or ""),
        "roles": _list_claim(claims.get("roles")),
        "groups": _list_claim(claims.get("groups")),
        "auth_provider": str(claims.get("provider") or "authnode"),
        "expires_at": str(data.get("expires_at") or ""),
    }


def session_from_public_user(
    user: Mapping[str, Any],
    *,
    requested_user_key: str,
    requested_tenant_id: str,
    ttl_seconds: int,
    auth_provider: str = "authnode-catalog",
) -> dict[str, Any]:
    now = int(time.time())
    user_key = str(user.get("user_key") or requested_user_key)
    user_id = str(user.get("user_id") or _normalized_user_id({"user_key": user_key}, fallback=requested_user_key))
    tenant_id = str(user.get("tenant_id") or user.get("tenant_key") or requested_tenant_id)
    return {
        "token": "",
        "exp": now + int(ttl_seconds),
        "iat": now,
        "tenant_id": tenant_id,
        "user_id": user_id.removeprefix("pska:"),
        "represented_user_id": user_id.removeprefix("pska:"),
        "subject": user_key,
        "display_name": str(user.get("display_name") or user.get("name") or ""),
        "email": str(user.get("email") or ""),
        "roles": _list_claim(user.get("roles")),
        "groups": _list_claim(user.get("groups")),
        "auth_provider": auth_provider,
        "expires_at": "",
    }


def session_from_callback_headers(
    headers: Mapping[str, str],
    config: GatewayConfig,
    *,
    requested_user_key: str | None = None,
    requested_tenant_id: str | None = None,
) -> dict[str, Any]:
    token = _bearer_token(_header_value(headers, "Authorization"))
    if token and config.callback_jwt_secret:
        try:
            claims = verify_hs256_jwt(
                token,
                config.callback_jwt_secret,
                issuer=config.callback_jwt_issuer,
                audience=config.callback_jwt_audience,
            )
        except AuthError as exc:
            raise GatewayError(str(exc)) from exc
        return session_from_token_response(
            {"access_token": token, "claims": claims},
            requested_user_key=requested_user_key or str(claims.get("user_key") or claims.get("sub") or config.default_user_key),
            requested_tenant_id=requested_tenant_id or _normalized_tenant_id(claims, fallback=config.default_tenant_id),
        )

    user_id = _header_value(headers, "X-PSKA-User-Id")
    tenant_id = _header_value(headers, "X-PSKA-Tenant-Id")
    if not user_id or not tenant_id:
        raise GatewayError("AuthNode callback requires a verified JWT or trusted PSKA identity headers")
    return session_from_public_user(
        {
            "user_id": user_id,
            "user_key": _header_value(headers, "X-PSKA-Subject") or requested_user_key or user_id,
            "tenant_id": tenant_id,
            "display_name": _header_value(headers, "X-PSKA-Display-Name"),
            "email": _header_value(headers, "X-PSKA-Email"),
            "roles": _header_value(headers, "X-PSKA-Roles"),
            "groups": _header_value(headers, "X-PSKA-Groups"),
        },
        requested_user_key=requested_user_key or user_id,
        requested_tenant_id=requested_tenant_id or tenant_id,
        ttl_seconds=config.token_ttl_seconds,
        auth_provider=_header_value(headers, "X-PSKA-Auth-Provider") or "authnode-callback",
    )


def public_session(session: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "authenticated": True,
        "tenant_id": session.get("tenant_id") or "",
        "user_id": session.get("user_id") or "",
        "represented_user_id": session.get("represented_user_id") or session.get("user_id") or "",
        "subject": session.get("subject") or "",
        "display_name": session.get("display_name") or "",
        "email": session.get("email") or "",
        "roles": _list_claim(session.get("roles")),
        "groups": _list_claim(session.get("groups")),
        "auth_provider": session.get("auth_provider") or "authnode",
        "expires_at": session.get("expires_at") or "",
        "exp": session.get("exp"),
    }


def authnode_logout_redirect(config: GatewayConfig, *, return_to: str) -> str:
    if not config.authnode_browser_login or not config.authnode_logout:
        return "/login"
    params = {"return_to": return_to}
    return f"{_authnode_browser_base_url(config)}/logout?{urlencode(params)}"


def _authnode_browser_base_url(config: GatewayConfig) -> str:
    return (config.authnode_browser_url or config.authnode_url).rstrip("/")


def proxy_request_headers(incoming: Mapping[str, str], session: Mapping[str, Any], config: GatewayConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in incoming.items():
        lowered = name.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in IDENTITY_HEADERS:
            continue
        if any(lowered.startswith(prefix) for prefix in IDENTITY_HEADER_PREFIXES):
            continue
        headers[name] = value
    token = str(session.get("token") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if config.pska_service_token:
        headers["X-PSKA-Service-Token"] = config.pska_service_token
    headers["X-PSKA-Tenant-Id"] = str(session.get("tenant_id") or "")
    headers["X-PSKA-User-Id"] = str(session.get("user_id") or "")
    headers["X-PSKA-Represented-User-Id"] = str(session.get("represented_user_id") or session.get("user_id") or "")
    headers["X-PSKA-Subject"] = str(session.get("subject") or session.get("user_id") or "")
    headers["X-PSKA-Display-Name"] = str(session.get("display_name") or "")
    headers["X-PSKA-Email"] = str(session.get("email") or "")
    headers["X-PSKA-Groups"] = ",".join(_list_claim(session.get("groups")))
    headers["X-PSKA-Roles"] = ",".join(_list_claim(session.get("roles")))
    headers["X-PSKA-Auth-Provider"] = "authnode-gateway"
    headers["X-PSKA-Caller"] = "user"
    return headers


class PSKAGatewayHandler(BaseHTTPRequestHandler):
    config: GatewayConfig

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._json(200, {"ok": True, "component": "pska-gateway"})
        if parsed.path == "/auth/session":
            return self._session_endpoint()
        if parsed.path == "/auth/callback":
            return self._auth_callback(parse_qs(parsed.query))
        if parsed.path == "/login":
            return self._login_form(parse_qs(parsed.query))
        if parsed.path == "/logout":
            return self._logout()
        session = self._session()
        if session is None:
            if _is_api_path(parsed.path):
                return self._json(401, {"error": "PSKA gateway session required", "login": "/login"})
            return self._redirect(_login_url(self.path), 302)
        if _is_api_path(parsed.path):
            return self._proxy(session)
        return self._static_or_index(parsed.path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            return self._login_submit()
        if parsed.path == "/logout":
            return self._logout()
        session = self._session()
        if session is None:
            return self._json(401, {"error": "PSKA gateway session required", "login": "/login"})
        return self._proxy(session)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        return self._method_proxy()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        return self._method_proxy()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        return self._method_proxy()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _method_proxy(self) -> None:
        session = self._session()
        if session is None:
            return self._json(401, {"error": "PSKA gateway session required", "login": "/login"})
        return self._proxy(session)

    def _session_endpoint(self) -> None:
        session = self._session()
        if session is None:
            return self._json(401, {"authenticated": False})
        return self._json(200, public_session(session))

    def _auth_callback(self, query: Mapping[str, list[str]]) -> None:
        next_path = _safe_next(_first(query.get("next")) or "/")
        try:
            code = (_first(query.get("code")) or "").strip()
            if code:
                token_data = request_authnode_code_exchange(self.config, code=code, target=DEFAULT_PSKA_AUDIENCE)
                next_path = _safe_next(_first(query.get("next")) or str(token_data.get("next") or "/"))
                claims = token_data.get("claims") if isinstance(token_data.get("claims"), dict) else {}
                session = session_from_token_response(
                    token_data,
                    requested_user_key=str(claims.get("user_key") or claims.get("sub") or self.config.default_user_key),
                    requested_tenant_id=_normalized_tenant_id(claims, fallback=self.config.default_tenant_id),
                )
            else:
                session = session_from_callback_headers(
                    self.headers,
                    self.config,
                    requested_user_key=_first(query.get("user_key")),
                    requested_tenant_id=_first(query.get("tenant_id")),
                )
        except GatewayError as exc:
            return self._html(401, f"<h1>AuthNode callback rejected</h1><p>{html.escape(str(exc))}</p>")
        return self._set_session_and_redirect(session, next_path)

    def _login_form(self, query: Mapping[str, list[str]]) -> None:
        next_path = _safe_next(_first(query.get("next")) or "/")
        session = self._session()
        if session is not None:
            return self._redirect(next_path, 302)
        if self.config.authnode_browser_login and not _truthy(_first(query.get("local"))):
            return self._redirect(self._authnode_login_url(query, next_path), 302)
        user_key = html.escape(_first(query.get("user_key")) or self.config.default_user_key)
        tenant_id = html.escape(_first(query.get("tenant_id")) or self.config.default_tenant_id)
        escaped_next = html.escape(next_path, quote=True)
        body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Login</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f5f0; color: #171717; }}
    main {{ width: min(420px, calc(100vw - 32px)); border: 1px solid #d9d6cc; border-radius: 8px; background: #fffefa; padding: 28px; box-shadow: 0 18px 50px rgba(28, 31, 35, 0.10); }}
    h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }}
    p {{ margin: 0 0 22px; color: #666; line-height: 1.5; }}
    label {{ display: grid; gap: 8px; margin: 16px 0; font-size: 13px; color: #555; }}
    input {{ height: 40px; border: 1px solid #cbc7ba; border-radius: 7px; padding: 0 12px; font: inherit; }}
    button {{ width: 100%; height: 42px; margin-top: 8px; border: 0; border-radius: 7px; background: #1f5f55; color: white; font-weight: 700; cursor: pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>PSKA</h1>
    <p>通过 AuthNode 签发短期 PSKA 身份，本页只用于本地和网关开发流程。</p>
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{escaped_next}">
      <label>Tenant <input name="tenant_id" value="{tenant_id}" autocomplete="organization" required></label>
      <label>User key <input name="user_key" value="{user_key}" autocomplete="username" required></label>
      <button type="submit">进入工作台</button>
    </form>
  </main>
</body>
</html>"""
        return self._html(200, body)

    def _login_submit(self) -> None:
        form = self._read_form()
        next_path = _safe_next(_first(form.get("next")) or "/")
        user_key = (_first(form.get("user_key")) or "").strip()
        tenant_id = (_first(form.get("tenant_id")) or "").strip()
        if not user_key or not tenant_id:
            return self._html(400, "<h1>Missing tenant_id or user_key</h1>")
        if self.config.authnode_admin_token:
            try:
                token_data = request_authnode_token(self.config, user_key=user_key, tenant_id=tenant_id)
                session = session_from_token_response(token_data, requested_user_key=user_key, requested_tenant_id=tenant_id)
            except GatewayError as exc:
                return self._html(502, f"<h1>AuthNode token request failed</h1><p>{html.escape(str(exc))}</p>")
        elif self.config.local_authnode_catalog_login:
            try:
                user = request_authnode_public_user(self.config, user_key=user_key, tenant_id=tenant_id)
                session = session_from_public_user(
                    user,
                    requested_user_key=user_key,
                    requested_tenant_id=tenant_id,
                    ttl_seconds=self.config.token_ttl_seconds,
                )
            except GatewayError as exc:
                return self._html(502, f"<h1>AuthNode local login failed</h1><p>{html.escape(str(exc))}</p>")
        else:
            return self._html(
                503,
                "<h1>Gateway login is not configured</h1>"
                "<p>Use an upstream AuthNode/OIDC login in production, configure AuthNode callback, or enable local AuthNode catalog login.</p>",
            )
        return self._set_session_and_redirect(session, next_path)

    def _set_session_and_redirect(self, session: Mapping[str, Any], next_path: str) -> None:
        cookie_value = encode_session(session, str(self.config.session_secret))
        self.send_response(302)
        self.send_header("Location", next_path)
        self.send_header("Set-Cookie", self._session_cookie(cookie_value, max_age=max(1, int(session["exp"]) - int(time.time()))))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authnode_login_url(self, query: Mapping[str, list[str]], next_path: str) -> str:
        return_to = self._external_url("/auth/callback")
        params = {
            "target": DEFAULT_PSKA_AUDIENCE,
            "return_to": return_to,
            "next": next_path,
            "user_key": _first(query.get("user_key")) or self.config.default_user_key,
            "tenant_id": _first(query.get("tenant_id")) or self.config.default_tenant_id,
        }
        return f"{_authnode_browser_base_url(self.config)}/login?{urlencode(params)}"

    def _external_url(self, path: str) -> str:
        proto = self.headers.get("X-Forwarded-Proto") or ("https" if self.config.cookie_secure else "http")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"{self.config.host}:{self.config.port}"
        return f"{proto}://{host}{path}"

    def _logout(self) -> None:
        location = authnode_logout_redirect(self.config, return_to=self._external_url("/login"))
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", self._clear_cookie())
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _proxy(self, session: Mapping[str, Any]) -> None:
        body = self._read_body()
        target = f"{self.config.pska_url.rstrip('/')}{self.path}"
        request = Request(
            target,
            data=body if self.command not in {"GET", "HEAD"} else None,
            headers=proxy_request_headers(self.headers, session, self.config),
            method=self.command,
        )
        upstream_timeout = _proxy_timeout_for_path(self.path, self.config)
        try:
            with urlopen(request, timeout=upstream_timeout) as response:
                if _is_event_stream_headers(response.headers) or _is_streaming_api_path(urlparse(self.path).path):
                    return self._relay_streaming_response(response.status, response.headers, response)
                return self._relay_response(response.status, response.headers, response.read())
        except HTTPError as exc:
            return self._relay_response(exc.code, exc.headers, exc.read())
        except URLError as exc:
            return self._json(502, {"error": f"PSKA service unavailable: {exc.reason}"})
        except TimeoutError:
            return self._json(504, {"error": "PSKA service timed out"})

    def _static_or_index(self, request_path: str) -> None:
        dist = self.config.frontend_dist.resolve()
        target = _static_target(dist, request_path)
        if target is None:
            return self._json(403, {"error": "invalid static path"})
        if not target.exists() and request_path not in {"/", ""}:
            return self._json(404, {"error": f"not found: {request_path}"})
        if not target.exists():
            return self._html(
                503,
                "<h1>PSKA frontend build missing</h1>"
                "<p>Run <code>cd frontend && npm run build</code>, or set <code>PSKA_GATEWAY_FRONTEND_DIST</code>.</p>",
            )
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if target.name == "index.html" else "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _session(self) -> dict[str, Any] | None:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get(self.config.cookie_name)
        value = morsel.value if morsel else None
        return decode_session(value, str(self.config.session_secret))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or "0")
        return self.rfile.read(length) if length else b""

    def _read_form(self) -> dict[str, list[str]]:
        return parse_qs(self._read_body().decode("utf-8"), keep_blank_values=True)

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, status: int) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _relay_response(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP_HEADERS or lowered == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay_streaming_response(self, status: int, headers: Mapping[str, str], response: Any) -> None:
        self.send_response(status)
        for name, value in headers.items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP_HEADERS or lowered == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.flush()
        for chunk in _iter_streaming_response_chunks(response, event_stream=_is_event_stream_headers(headers)):
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except BrokenPipeError:
                return

    def _session_cookie(self, value: str, *, max_age: int) -> str:
        parts = [
            f"{self.config.cookie_name}={quote(value)}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if self.config.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _clear_cookie(self) -> str:
        parts = [
            f"{self.config.cookie_name}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
        if self.config.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)


def _is_api_path(path: str) -> bool:
    return path in {
        "/ready",
        "/index-status",
        "/metrics",
        "/mcp",
        "/search",
        "/agentic-search",
    } or path.startswith(
        (
            "/workspace/",
            "/console/",
            "/review-items",
            "/connectors/",
            "/jobs",
            "/digest/",
            "/knowledge-sources/",
            "/files/",
            "/ingest/",
            "/extract/",
            "/profile/",
            "/candidates",
        )
    )


def _is_event_stream_headers(headers: Mapping[str, str]) -> bool:
    content_type = ""
    try:
        content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
    except AttributeError:
        content_type = ""
    if not content_type and hasattr(headers, "items"):
        for name, value in headers.items():
            if str(name).lower() == "content-type":
                content_type = str(value)
                break
    return "text/event-stream" in content_type.lower()


def _is_streaming_api_path(path: str) -> bool:
    return path in {"/workspace/ask/stream"}


def _is_long_running_api_path(path: str) -> bool:
    return path in {
        "/workspace/ask",
        "/workspace/ask/stream",
        "/workspace/digest/run",
        "/workspace/sources/upload",
    }


def _iter_streaming_response_chunks(response: Any, *, event_stream: bool):
    if event_stream and hasattr(response, "readline"):
        while True:
            chunk = response.readline()
            if not chunk:
                break
            yield chunk
        return
    while True:
        chunk = response.read(8192)
        if not chunk:
            break
        yield chunk


def _proxy_timeout_for_path(path: str, config: GatewayConfig) -> float:
    if _is_long_running_api_path(urlparse(path).path):
        return max(float(config.request_timeout_seconds), 300.0)
    return float(config.request_timeout_seconds)


def _static_target(dist: Path, request_path: str) -> Path | None:
    relative = request_path.lstrip("/") or "index.html"
    candidate = (dist / relative).resolve()
    if candidate.is_dir():
        candidate = candidate / "index.html"
    try:
        candidate.relative_to(dist)
    except ValueError:
        return None
    if not candidate.exists() and not Path(relative).suffix:
        candidate = dist / "index.html"
    return candidate


def _login_url(next_path: str) -> str:
    return f"/login?{urlencode({'next': _safe_next(next_path)})}"


def _safe_next(value: str) -> str:
    parsed = urlparse(value or "/")
    if parsed.scheme or parsed.netloc:
        return "/"
    path = parsed.path or "/"
    if not path.startswith("/"):
        return "/"
    return path + (f"?{parsed.query}" if parsed.query else "")


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def _header_value(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value:
        return str(value).strip()
    lowered = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lowered:
            return str(candidate).strip()
    return ""


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    return value[len(prefix) :].strip() if value.startswith(prefix) else None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_url_env(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value.rstrip("/") or None


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def _normalized_user_id(claims: Mapping[str, Any], *, fallback: str) -> str:
    value = str(claims.get("user_id") or "").strip()
    if value:
        return value.removeprefix("pska:")
    for key in ("user_key", "sub"):
        candidate = str(claims.get(key) or "").strip()
        if candidate.startswith("pska:"):
            return candidate.removeprefix("pska:")
        if candidate:
            return candidate
    return fallback.removeprefix("pska:")


def _normalized_tenant_id(claims: Mapping[str, Any], *, fallback: str) -> str:
    for key in ("tenant_id", "tenant_key", "tenant", "org_id"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return fallback


def _list_claim(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []
