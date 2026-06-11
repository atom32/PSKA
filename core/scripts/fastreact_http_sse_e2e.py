from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FASTREACT_SRC_CANDIDATES = [
    Path.home() / "FastReAct" / "fastreact-nano" / "src",
    Path.home() / "Fastreact" / "fastreact-nano" / "src",
]
SERVICE_TOKEN = "pska-fastreact-e2e-token"


class PSKAMCPBridgeAgent:
    """Deterministic FastReAct test agent that calls a real PSKA MCP server."""

    skills: dict[str, Any] = {}

    def __init__(self, python: str) -> None:
        self.python = python
        self.process: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.tools: list[str] = []

    async def ensure_mcp_loaded(self, required_skills: list[str] | None = None) -> dict[str, Any]:
        del required_skills
        self._ensure_process()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        response = self._request("tools/list", {})
        self.tools = [tool["name"] for tool in response["result"]["tools"]]
        return {"loaded": True, "tools": self.tools}

    def list_mcp_tools(self) -> list[str]:
        return [f"pska_{tool}" for tool in self.tools] if self.tools else ["pska_pska_search"]

    def list_mcp_server_status(self) -> list[dict[str, Any]]:
        return [{"name": "pska", "alive": self.process is not None and self.process.poll() is None}]

    def list_skills(self) -> list[str]:
        return []

    def list_tools(self) -> list[str]:
        return self.list_mcp_tools()

    async def run(self, query: str) -> str:
        return f"PSKA answer for: {query}"

    async def run_event_stream(
        self,
        query: str,
        skills: list[str] | None = None,
        session_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_key: str | None = None,
    ):
        del skills, history, user_key
        from fastreact.core.events import AgentEvent

        session_id = session_id or "pska-fastreact-e2e-session"
        await self.ensure_mcp_loaded()
        tool_args = {"query": query, "user_id": "user_primary", "top_k": 3}
        yield AgentEvent.session_start(query, session_id, metadata={"caller": "pska-e2e"})
        yield AgentEvent.tool_call("pska_pska_search", tool_args, session_id, call_id="pska-call-1")
        tool_payload = self._call_tool("pska_search", tool_args)
        tool_text = tool_payload["content"][0]["text"]
        yield AgentEvent.tool_result("pska_pska_search", tool_text, session_id)
        parsed = json.loads(tool_text)
        citations = parsed.get("citations") or []
        title = citations[0].get("title") if citations else "PSKA source"
        yield AgentEvent.session_end(session_id, f"PSKA MCP returned evidence from {title}.")

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _ensure_process(self) -> None:
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        self.process = subprocess.Popen(
            [self.python, "-u", "-c", _mcp_server_snippet()],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        response = self._request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fastreact-e2e"}},
        )
        if "result" not in response:
            raise RuntimeError(f"PSKA MCP initialize failed: {response}")

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._request("tools/call", {"name": name, "arguments": arguments})
        return response["result"]

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self.process and self.process.stdout
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise RuntimeError(f"PSKA MCP closed before response. stderr={stderr}")
        return json.loads(line)

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PSKA <-> FastReAct HTTP/SSE E2E contract smoke.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    fastreact_src = _resolve_fastreact_src()
    sys.path.insert(0, str(fastreact_src))
    os.environ["FASTREACT_SERVICE_TOKEN"] = SERVICE_TOKEN

    from fastreact.adapters.http import create_app, set_agent_for_testing
    import uvicorn

    agent = PSKAMCPBridgeAgent(args.python)
    set_agent_for_testing(agent)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        _wait_for_http(port, args.timeout)
        ready = _json_request(
            f"http://127.0.0.1:{port}/ready",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            timeout=args.timeout,
        )
        response_text, response_headers = _sse_request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            {
                "messages": [
                    {"role": "system", "content": "Use PSKA MCP tools and cite evidence."},
                    {"role": "user", "content": "Find the FastReAct PSKA evidence note"},
                ],
                "stream": True,
                "session_id": "pska-fastreact-e2e-session",
                "user_key": "pska:user_primary",
                "metadata": {"caller": "pska", "run_id": "pska-fastreact-e2e", "purpose": "e2e"},
            },
            headers={"X-FastReAct-Service-Token": SERVICE_TOKEN},
            timeout=args.timeout,
        )
        events = _parse_sse(response_text)
        result = _assert_contract(ready, events, response_headers)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        agent.close()
        set_agent_for_testing(None)


def _assert_contract(ready: dict[str, Any], events: list[dict[str, Any]], headers: Any) -> dict[str, Any]:
    event_types = [event["type"] for event in events if event.get("type") != "done"]
    if ready["auth"]["required"] is not True:
        raise AssertionError(f"readiness auth contract failed: {ready}")
    if ready["service_contract"] != "fastreact.agent_event.v1":
        raise AssertionError(f"unexpected schema: {ready}")
    if ready["mcp"]["ready"] is not True or "pska_pska_search" not in ready["mcp"]["tools"]:
        raise AssertionError(f"PSKA MCP not ready: {ready}")
    for required_type in ("session_start", "tool_call", "tool_result", "session_end"):
        if required_type not in event_types:
            raise AssertionError(f"missing SSE event {required_type}: {event_types}")
    if "done" not in [event.get("type") for event in events]:
        raise AssertionError("missing SSE done event")
    tool_result = next(event for event in events if event["type"] == "tool_result")
    if "FastReAct PSKA evidence note" not in tool_result.get("content", ""):
        raise AssertionError(f"tool result did not include seeded PSKA evidence: {tool_result}")
    return {
        "ok": True,
        "ready": ready,
        "event_types": event_types,
        "schema_header": headers.get("x-fastreact-event-schema"),
        "run_id_header": headers.get("x-fastreact-run-id"),
    }


def _json_request(url: str, *, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _sse_request(url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout: float) -> tuple[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8"), response.headers


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in text.strip().split("\n\n"):
        event_type = "message"
        data = ""
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_type = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            events.append({"type": "done"})
        elif data:
            payload = json.loads(data)
            payload.setdefault("type", event_type)
            events.append(payload)
    return events


def _wait_for_http(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _json_request(f"http://127.0.0.1:{port}/health", headers={}, timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(f"FastReAct HTTP server did not start on port {port}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_fastreact_src() -> Path:
    for candidate in FASTREACT_SRC_CANDIDATES:
        if (candidate / "fastreact").exists():
            return candidate
    raise FileNotFoundError("FastReAct fastreact-nano src not found")


def _mcp_server_snippet() -> str:
    return r'''
from pska_core.enums import UserRole, Visibility
from pska_core.ingest import IngestService
from pska_core.mcp_server import MCPServer
from pska_core.models import TeamMembership, User
from pska_core.store import InMemoryKnowledgeStore

store = InMemoryKnowledgeStore()
store.add_user(User("user_primary", "primary", UserRole.ADMIN))
store.add_team_membership(TeamMembership("user_primary", "team_default"))
IngestService(store).ingest_channel_payload(
    {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "note",
        "source_id": "fastreact-pska-e2e",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "title": "FastReAct PSKA evidence note",
        "content": {"text": "FastReAct can call the PSKA MCP search tool over HTTP/SSE E2E."},
    }
)
raise SystemExit(MCPServer("memory://e2e", store=store).run())
'''


if __name__ == "__main__":
    raise SystemExit(main())
