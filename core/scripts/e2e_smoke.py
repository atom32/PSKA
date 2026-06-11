from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SMOKE_DATABASE_URL = "postgresql:///pska_smoke"


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env.setdefault("PSKA_DATABASE_URL", SMOKE_DATABASE_URL)
    api_key_path = Path.home() / "api_key.txt"
    if api_key_path.exists():
        env.setdefault("PSKA_LLM_API_KEY_FILE", str(api_key_path))
        first_line = _read_api_key_first_line(api_key_path)
        if first_line:
            env.setdefault("FASTRACT_API_KEY", first_line)
            env.setdefault("OPENAI_API_KEY", first_line)

    report: dict[str, Any] = {}
    steps = [
        ["python3", "-m", "pska_core.cli", "db-reset", "--name", "pska_smoke"],
        [
            "python3",
            "-m",
            "pska_core.cli",
            "--database-url",
            SMOKE_DATABASE_URL,
            "import-twitter-zips",
            "--input",
            str(Path.home() / "Downloads" / "twitter_archive"),
            "--archive-root",
            str(ROOT / "archive" / "imports"),
        ],
        [
            "python3",
            "-m",
            "pska_core.cli",
            "--database-url",
            SMOKE_DATABASE_URL,
            "extract-all",
            "--owner-user-id",
            "user_primary",
        ],
    ]
    for step in steps:
        result = subprocess.run(step, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        report[" ".join(step[:4])] = _step_result(result)
        if result.returncode != 0:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return result.returncode

    query = _sample_query(env)
    search = subprocess.run(
        [
            "python3",
            "-m",
            "pska_core.cli",
            "--database-url",
            SMOKE_DATABASE_URL,
            "search",
            "--query",
            query,
            "--user-id",
            "user_primary",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    report["cli_search"] = _step_result(search)
    if search.returncode != 0:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return search.returncode

    mcp = _mcp_call(env, query)
    report["mcp_search"] = mcp
    agentic = subprocess.run(
        [
            "python3",
            "-m",
            "pska_core.cli",
            "--database-url",
            SMOKE_DATABASE_URL,
            "agentic-search",
            "--query",
            query,
            "--user-id",
            "user_primary",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    report["agentic_search"] = _step_result(agentic)
    if agentic.returncode != 0:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return agentic.returncode
    api = _api_smoke(env, query)
    report["http_api"] = api
    report["fastreact_smoke"] = _fastreact_mcp_smoke(env, query)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if mcp.get("ok") and api.get("ok") and report["fastreact_smoke"].get("ok") else 1


def _sample_query(env: dict[str, str]) -> str:
    try:
        import psycopg

        with psycopg.connect(SMOKE_DATABASE_URL) as conn:
            row = conn.execute("select content_text from source_items where content_text <> '' limit 1").fetchone()
        text = row[0] if row else ""
        if "GitHub" in text:
            return "GitHub"
        tokens = re.findall(r"[A-Za-z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}", text)
        if tokens:
            return tokens[0]
    except Exception:
        pass
    return "archive"


def _mcp_call(env: dict[str, str], query: str) -> dict[str, Any]:
    process = subprocess.Popen(
        [str(ROOT.parent / "scripts" / "pska"), "mcp-server"],
        cwd=ROOT,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin and process.stdout
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "pska_search", "arguments": {"query": query, "user_id": "user_primary", "top_k": 5}},
        },
    ]
    responses = []
    for request in requests:
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        if "id" in request:
            responses.append(json.loads(process.stdout.readline()))
    process.stdin.close()
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
    call = responses[-1]
    return {"ok": "result" in call, "responses": responses}


def _api_smoke(env: dict[str, str], query: str) -> dict[str, Any]:
    process = subprocess.Popen(
        ["python3", "-m", "pska_core.cli", "--database-url", SMOKE_DATABASE_URL, "serve", "--port", "8766"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(30):
            try:
                with urlopen("http://127.0.0.1:8766/health", timeout=0.5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.1)
        else:
            return {"ok": False, "error": "server did not start"}
        request = Request(
            "http://127.0.0.1:8766/agentic-search",
            data=json.dumps({"query": query, "user_id": "user_primary"}).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"ok": bool(payload.get("retrieval", {}).get("citations") or payload.get("answer")), "health": health, "answer": payload.get("answer")}
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def _fastreact_mcp_smoke(env: dict[str, str], query: str) -> dict[str, Any]:
    fastreact_src = _resolve_fastreact_src(env)
    if not fastreact_src.exists():
        return {"ok": False, "error": f"Fastreact source not found: {fastreact_src}"}

    smoke_env = env.copy()
    smoke_env["PYTHONPATH"] = f"{SRC}:{fastreact_src}"
    smoke_env["PSKA_DATABASE_URL"] = SMOKE_DATABASE_URL
    smoke_env["FASTRACT_MCP_SERVERS"] = json.dumps(
        [
            {
                "name": "pska",
                "command": str(ROOT.parent / "scripts" / "pska"),
                "args": ["mcp-server"],
                "isolation": "shared",
                "description": "Read-only PSKA personal knowledge store tools.",
            }
        ]
    )
    api_key_path = Path.home() / "api_key.txt"
    if api_key_path.exists():
        smoke_env.setdefault("PSKA_LLM_API_KEY_FILE", str(api_key_path))
        key = _read_api_key_first_line(api_key_path)
        if key:
            smoke_env.setdefault("FASTRACT_API_KEY", key)
            smoke_env.setdefault("OPENAI_API_KEY", key)

    snippet = r'''
import asyncio
import json
from fastreact import Agent
from fastreact.core.config import Config

async def main():
    agent = Agent(config=Config.from_env())
    await agent._load_mcp_servers()
    tools = [name for name in agent._tools.list_all() if name.startswith("pska_")]
    wrapper = agent._tools.get("pska_pska_search")
    search_text = await wrapper.execute(query=QUERY, user_id="user_primary", top_k=3)
    if agent._mcp_manager:
        await agent._mcp_manager.close_all()
    print(json.dumps({"tools": tools, "search": json.loads(search_text)}, ensure_ascii=False))

asyncio.run(main())
'''.replace("QUERY", repr(query))
    result = subprocess.run(
        ["python3", "-c", snippet],
        cwd=ROOT,
        env=smoke_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    output = _step_result(result)
    if result.returncode != 0:
        return {"ok": False, **output}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid fastreact smoke JSON: {exc}", **output}
    required = {"pska_pska_search", "pska_pska_agentic_search", "pska_pska_index_status"}
    tools = set(payload.get("tools", []))
    return {
        "ok": required.issubset(tools),
        "tools": payload.get("tools", []),
        "search_has_response": isinstance(payload.get("search"), dict),
        "stdout": _scrub(result.stdout),
        "stderr": _scrub(result.stderr),
    }


def _step_result(result: subprocess.CompletedProcess) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stdout": _scrub(result.stdout),
        "stderr": _scrub(result.stderr),
    }


def _read_api_key_first_line(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def _scrub(text: str) -> str:
    key = os.environ.get("FASTRACT_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if key:
        text = text.replace(key, "***")
    return text[-4000:]


def _resolve_fastreact_src(env: dict[str, str]) -> Path:
    configured = env.get("FASTREACT_SRC")
    if configured:
        return Path(configured).expanduser()

    candidates = [
        Path.home() / "FastReAct" / "fastreact-nano" / "src",
        Path.home() / "Fastreact" / "fastreact-nano" / "src",
        Path.home() / "FastReact" / "fastreact-nano" / "src",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


if __name__ == "__main__":
    raise SystemExit(main())
