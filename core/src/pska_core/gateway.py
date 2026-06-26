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
    authnode_admin_token: str | None = None
    pska_service_token: str | None = None
    session_secret: str | None = None
    cookie_name: str = DEFAULT_SESSION_COOKIE
    cookie_secure: bool = False
    token_ttl_seconds: int = 3600
    request_timeout_seconds: float = 15.0
    default_tenant_id: str = "tenant_default"
    default_user_key: str = "pska:user_primary"

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        secret = os.getenv("PSKA_GATEWAY_SESSION_SECRET")
        return cls(
            host=os.getenv("PSKA_GATEWAY_HOST", "127.0.0.1"),
            port=_int_env("PSKA_GATEWAY_PORT", 8080),
            frontend_dist=Path(os.getenv("PSKA_GATEWAY_FRONTEND_DIST", str(DEFAULT_FRONTEND_DIST))).expanduser(),
            pska_url=os.getenv("PSKA_GATEWAY_PSKA_URL", "http://127.0.0.1:8765").rstrip("/"),
            authnode_url=(os.getenv("PSKA_GATEWAY_AUTHNODE_URL") or os.getenv("AUTHNODE_URL") or "http://127.0.0.1:8788").rstrip("/"),
            authnode_admin_token=os.getenv("PSKA_GATEWAY_AUTHNODE_ADMIN_TOKEN") or os.getenv("AUTHNODE_ADMIN_TOKEN") or None,
            pska_service_token=os.getenv("PSKA_GATEWAY_PSKA_SERVICE_TOKEN") or None,
            session_secret=secret,
            cookie_name=os.getenv("PSKA_GATEWAY_COOKIE_NAME", DEFAULT_SESSION_COOKIE),
            cookie_secure=_bool_env("PSKA_GATEWAY_COOKIE_SECURE", False),
            token_ttl_seconds=_int_env("PSKA_GATEWAY_TOKEN_TTL_SECONDS", 3600),
            request_timeout_seconds=float(os.getenv("PSKA_GATEWAY_REQUEST_TIMEOUT_SECONDS", "15")),
            default_tenant_id=os.getenv("PSKA_GATEWAY_DEFAULT_TENANT_ID", "tenant_default"),
            default_user_key=os.getenv("PSKA_GATEWAY_DEFAULT_USER_KEY", "pska:user_primary"),
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
            authnode_admin_token=self.authnode_admin_token,
            pska_service_token=self.pska_service_token,
            session_secret=secrets.token_urlsafe(48),
            cookie_name=self.cookie_name,
            cookie_secure=self.cookie_secure,
            token_ttl_seconds=self.token_ttl_seconds,
            request_timeout_seconds=self.request_timeout_seconds,
            default_tenant_id=self.default_tenant_id,
            default_user_key=self.default_user_key,
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

    def _login_form(self, query: Mapping[str, list[str]]) -> None:
        next_path = _safe_next(_first(query.get("next")) or "/")
        session = self._session()
        if session is not None:
            return self._redirect(next_path, 302)
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
        else:
            return self._html(
                503,
                "<h1>Gateway login is not configured</h1>"
                "<p>Use an upstream AuthNode/OIDC login in production. The built-in login form is only a local token-broker shim.</p>",
            )
        cookie_value = encode_session(session, str(self.config.session_secret))
        self.send_response(302)
        self.send_header("Location", next_path)
        self.send_header("Set-Cookie", self._session_cookie(cookie_value, max_age=max(1, int(session["exp"]) - int(time.time()))))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _logout(self) -> None:
        self.send_response(302)
        self.send_header("Location", "/login")
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
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
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
