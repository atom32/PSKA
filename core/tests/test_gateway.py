from __future__ import annotations

import json
from typing import Any

from pska_core import gateway
from pska_core.gateway import (
    GatewayConfig,
    authnode_login_url,
    authnode_logout_redirect,
    decode_session,
    encode_session,
    gateway_external_url,
    _iter_streaming_response_chunks,
    _is_event_stream_headers,
    _is_streaming_api_path,
    _proxy_timeout_for_path,
    proxy_request_headers,
    request_authnode_code_exchange,
    request_authnode_token,
    session_from_callback_headers,
    session_from_token_response,
)


def test_gateway_session_round_trip_and_tamper_rejected() -> None:
    session = {
        "token": "jwt-pska",
        "tenant_id": "tenant_acme",
        "user_id": "ada",
        "subject": "pska:ada",
        "exp": 200,
    }

    cookie = encode_session(session, "secret")

    assert decode_session(cookie, "secret", now=100)["tenant_id"] == "tenant_acme"
    assert decode_session(cookie.rsplit(".", 1)[0] + ".tampered", "secret", now=100) is None
    assert decode_session(cookie, "wrong-secret", now=100) is None
    assert decode_session(cookie, "secret", now=201) is None


def test_gateway_requests_authnode_pska_audience(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout=0):  # noqa: ANN001 - urllib Request is enough for this contract test.
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {key.lower(): value for key, value in request.headers.items()},
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return FakeResponse(
            {
                "access_token": "jwt-pska",
                "expires_at": "2030-01-01T00:00:00+00:00",
                "claims": {
                    "sub": "pska:ada",
                    "tenant_id": "tenant_acme",
                    "user_id": "ada",
                    "roles": ["admin"],
                    "groups": ["research"],
                    "provider": "authnode",
                    "iat": 100,
                    "exp": 700,
                },
            }
        )

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)

    response = request_authnode_token(
        GatewayConfig(
            authnode_url="http://authnode.test",
            authnode_admin_token="admin-token",
            token_ttl_seconds=600,
            request_timeout_seconds=3,
        ),
        user_key="pska:ada",
        tenant_id="tenant_acme",
    )
    session = session_from_token_response(response, requested_user_key="pska:ada", requested_tenant_id="tenant_acme")

    assert len(calls) == 1
    assert calls[0]["url"] == "http://authnode.test/v1/token"
    assert calls[0]["method"] == "POST"
    assert calls[0]["headers"]["x-authnode-admin-token"] == "admin-token"
    assert calls[0]["payload"] == {
        "user_key": "pska:ada",
        "tenant_id": "tenant_acme",
        "audience": "pska",
        "ttl_seconds": 600,
    }
    assert calls[0]["timeout"] == 3
    assert session["token"] == "jwt-pska"
    assert session["tenant_id"] == "tenant_acme"
    assert session["user_id"] == "ada"
    assert session["roles"] == ["admin"]


def test_gateway_exchanges_authnode_browser_code(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout=0):  # noqa: ANN001 - urllib Request is enough for this contract test.
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {key.lower(): value for key, value in request.headers.items()},
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return FakeResponse(
            {
                "access_token": "jwt-from-code",
                "claims": {
                    "sub": "pska:ada",
                    "tenant_id": "tenant_acme",
                    "user_id": "ada",
                    "exp": 700,
                },
                "target": "pska",
                "next": "/",
            }
        )

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)

    response = request_authnode_code_exchange(
        GatewayConfig(
            authnode_url="http://authnode.internal",
            authnode_browser_url="http://authnode.public",
            request_timeout_seconds=4,
        ),
        code="one-time-code",
        target="pska",
    )

    assert response["access_token"] == "jwt-from-code"
    assert len(calls) == 1
    assert calls[0]["url"] == "http://authnode.internal/v1/auth/exchange"
    assert calls[0]["method"] == "POST"
    assert calls[0]["headers"]["content-type"] == "application/json"
    assert calls[0]["headers"]["accept"] == "application/json"
    assert calls[0]["payload"] == {"code": "one-time-code", "target": "pska"}
    assert calls[0]["timeout"] == 4


def test_gateway_logout_can_redirect_through_authnode() -> None:
    config = GatewayConfig(authnode_url="http://authnode.test", authnode_browser_login=True, authnode_logout=True)

    assert (
        authnode_logout_redirect(config, return_to="http://pska.test/login")
        == "http://authnode.test/logout?return_to=http%3A%2F%2Fpska.test%2Flogin"
    )
    assert authnode_logout_redirect(
        GatewayConfig(authnode_url="http://authnode.test", authnode_browser_login=False),
        return_to="http://pska.test/login",
    ) == "/login"
    assert authnode_logout_redirect(
        GatewayConfig(authnode_url="http://authnode.test", authnode_logout=False),
        return_to="http://pska.test/login",
    ) == "/login"


def test_gateway_redirects_use_public_authnode_browser_url(monkeypatch) -> None:
    monkeypatch.setenv("PSKA_GATEWAY_AUTHNODE_URL", "http://authnode.internal/")
    monkeypatch.setenv("PSKA_GATEWAY_AUTHNODE_BROWSER_URL", "http://authnode.public/")

    config = GatewayConfig.from_env()

    assert config.authnode_url == "http://authnode.internal"
    assert config.authnode_browser_url == "http://authnode.public"
    assert (
        authnode_logout_redirect(config, return_to="http://pska.test/login")
        == "http://authnode.public/logout?return_to=http%3A%2F%2Fpska.test%2Flogin"
    )


def test_gateway_login_url_uses_configured_authnode_base() -> None:
    config = GatewayConfig(
        authnode_url="http://authnode.internal",
        authnode_browser_url="https://login.example.test",
        default_user_key="pska:default",
        default_tenant_id="tenant_default",
    )

    assert (
        authnode_login_url(
            config,
            return_to="https://pska.example.test/auth/callback",
            next_path="/workspace?tab=ask",
            user_key="pska:alice",
            tenant_id="tenant_acme",
        )
        == "https://login.example.test/login?target=pska&return_to=https%3A%2F%2Fpska.example.test%2Fauth%2Fcallback&next=%2Fworkspace%3Ftab%3Dask&user_key=pska%3Aalice&tenant_id=tenant_acme"
    )


def test_gateway_external_url_prefers_configured_public_url() -> None:
    config = GatewayConfig(host="127.0.0.1", port=5173, public_url="https://pska.example.test/app")

    assert (
        gateway_external_url(
            config,
            {"Host": "127.0.0.1:5173", "X-Forwarded-Host": "wrong.example.test", "X-Forwarded-Proto": "http"},
            "/auth/callback",
        )
        == "https://pska.example.test/app/auth/callback"
    )


def test_gateway_external_url_uses_forwarded_headers_before_bind_host() -> None:
    config = GatewayConfig(host="127.0.0.1", port=5173)

    assert (
        gateway_external_url(
            config,
            {"Forwarded": "for=10.0.0.12;proto=https;host=pska.forwarded.test"},
            "/auth/callback",
        )
        == "https://pska.forwarded.test/auth/callback"
    )
    assert (
        gateway_external_url(
            config,
            {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "pska.proxy.test", "Host": "127.0.0.1:5173"},
            "/auth/callback",
        )
        == "https://pska.proxy.test/auth/callback"
    )


def test_gateway_callback_accepts_trusted_headers_without_browser_token() -> None:
    session = session_from_callback_headers(
        {
            "X-PSKA-User-Id": "ada",
            "X-PSKA-Tenant-Id": "tenant_acme",
            "X-PSKA-Subject": "pska:ada",
            "X-PSKA-Roles": "writer,reviewer",
            "X-PSKA-Groups": "research",
            "X-PSKA-Auth-Provider": "authnode",
        },
        GatewayConfig(token_ttl_seconds=600),
    )

    assert session["token"] == ""
    assert session["tenant_id"] == "tenant_acme"
    assert session["user_id"] == "ada"
    assert session["roles"] == ["writer", "reviewer"]
    assert session["auth_provider"] == "authnode"


def test_gateway_proxy_headers_strip_caller_identity_and_inject_session() -> None:
    headers = proxy_request_headers(
        {
            "Authorization": "Bearer caller-supplied",
            "Cookie": "pska_gateway_session=secret",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-PSKA-Tenant-Id": "tenant_wrong",
            "X-FastReAct-User-Key": "fastreact:wrong",
            "X-AuthNode-Admin-Token": "admin",
        },
        {
            "token": "jwt-pska",
            "tenant_id": "tenant_acme",
            "user_id": "ada",
            "represented_user_id": "ada",
            "subject": "pska:ada",
            "display_name": "Ada",
            "email": "ada@example.test",
            "roles": ["admin"],
            "groups": ["research"],
        },
        GatewayConfig(pska_service_token="service-token"),
    )

    assert headers["Authorization"] == "Bearer jwt-pska"
    assert "X-PSKA-Service-Token" not in headers
    assert headers["X-PSKA-Tenant-Id"] == "tenant_acme"
    assert headers["X-PSKA-User-Id"] == "ada"
    assert headers["X-PSKA-Auth-Provider"] == "authnode-gateway"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert "Cookie" not in headers
    assert "X-FastReAct-User-Key" not in headers
    assert headers["X-PSKA-Roles"] == "admin"


def test_gateway_detects_sse_response_headers_case_insensitively() -> None:
    assert _is_event_stream_headers({"Content-Type": "text/event-stream; charset=utf-8"}) is True
    assert _is_event_stream_headers({"content-type": "application/json"}) is False


def test_gateway_forces_ask_stream_to_streaming_timeout() -> None:
    config = GatewayConfig(request_timeout_seconds=15)
    conversation_stream_path = "/workspace/ask/conversations/ask_123/messages/stream"

    assert _is_streaming_api_path("/workspace/ask/stream") is True
    assert _is_streaming_api_path(conversation_stream_path) is True
    assert _is_streaming_api_path("/workspace/ask") is False
    assert _proxy_timeout_for_path("/workspace/ask/stream", config) == 300.0
    assert _proxy_timeout_for_path(conversation_stream_path, config) == 300.0
    assert _proxy_timeout_for_path("/workspace/ask", config) == 300.0
    assert _proxy_timeout_for_path("/workspace/sources/upload", config) == 300.0
    assert _proxy_timeout_for_path("/workspace/digest/run", config) == 300.0
    assert _proxy_timeout_for_path("/workspace/search/query", config) == 15.0


def test_gateway_streaming_relay_uses_sse_lines_instead_of_large_reads() -> None:
    response = FakeStreamResponse([b"event: route\n", b"data: {}\n", b"\n"])

    chunks = list(_iter_streaming_response_chunks(response, event_stream=True))

    assert chunks == [b"event: route\n", b"data: {}\n", b"\n"]
    assert response.readline_calls == 4
    assert response.read_calls == []


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class FakeStreamResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.readline_calls = 0
        self.read_calls: list[int] = []

    def readline(self) -> bytes:
        self.readline_calls += 1
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if not self._lines:
            return b""
        return self._lines.pop(0)
