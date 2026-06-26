from __future__ import annotations

import json
from typing import Any

from pska_core import gateway
from pska_core.gateway import GatewayConfig, decode_session, encode_session, proxy_request_headers, request_authnode_token, session_from_token_response


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
    assert headers["X-PSKA-Service-Token"] == "service-token"
    assert headers["X-PSKA-Tenant-Id"] == "tenant_acme"
    assert headers["X-PSKA-User-Id"] == "ada"
    assert headers["X-PSKA-Auth-Provider"] == "authnode-gateway"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert "Cookie" not in headers
    assert "X-FastReAct-User-Key" not in headers
    assert headers["X-PSKA-Roles"] == "admin"


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")
