from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row

from pska_core.keyfile import read_api_key_file as read_pska_api_key_file

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_DATABASE_URL = "postgresql:///pska_smoke"
DEFAULT_INPUT = Path.home() / "Downloads" / "twitter_archive"
DEFAULT_OUTPUT = ROOT / "reports" / "pska_twitter_full_test.html"
DEFAULT_JSON_OUTPUT = ROOT / "reports" / "pska_twitter_full_test.json"
DEFAULT_HISTORY_DIR = ROOT / "reports" / "runs"
FASTREACT_SRC = None

FIXED_QUESTIONS = [
    "这些归档里提到了哪些 AI 编程工具或自动化工具？",
    "哪些工具和 Codex、Claude Code 或 Fastreact 有关系？",
    "归档中有没有提到 GitHub star、开源项目或浏览器自动化？",
    "基于知识图谱，列出最重要的实体和它们之间的关系。",
]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = ReportRunner(args)
    report = runner.run()
    write_outputs(report, args.output, args.json_output)
    print(f"HTML report: {args.output}")
    print(f"JSON report: {args.json_output}")
    return 0 if report["run_metadata"]["overall_status"] == "passed" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a full PSKA + Fastreact Twitter archive HTML test report.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--archive-root", type=Path, default=ROOT / "archive" / "imports")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--owner-user-id", default="user_primary")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--api-port", type=int, default=8767)
    parser.add_argument("--fastreact-timeout", type=int, default=180)
    parser.add_argument("--fastreact-mode", choices=["auto", "api", "local"], default="auto")
    parser.add_argument("--fastreact-api-url", default=os.environ.get("FASTREACT_API_URL", "http://127.0.0.1:18741"))
    parser.add_argument("--fastreact-service-token", default=os.environ.get("FASTREACT_SERVICE_TOKEN"))
    parser.add_argument("--fastreact-model", default=os.environ.get("FASTRACT_MODEL", ""))
    parser.add_argument("--embedding-provider", default=os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"))
    parser.add_argument("--embedding-model", default=os.environ.get("PSKA_EMBEDDING_MODEL", "BAAI/bge-m3"))
    parser.add_argument("--embedding-dimensions", type=int, default=int(os.environ.get("PSKA_EMBEDDING_DIMENSIONS", "1024")))
    parser.add_argument("--skip-import", action="store_true", help="Skip DB reset, Twitter ZIP import, embedding, and extraction.")
    parser.add_argument("--only-fastreact", action="store_true", help="Only run Fastreact question paths after DB introspection.")
    return parser


class ReportRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = args.run_id or make_run_id()
        self.recovery_log = ROOT / "reports" / "pska_llm_recovery_events.jsonl"
        self.env = make_env(args.database_url, self.recovery_log)
        self.env["PSKA_EMBEDDING_PROVIDER"] = args.embedding_provider
        self.env["PSKA_EMBEDDING_MODEL"] = args.embedding_model
        self.env["PSKA_EMBEDDING_DIMENSIONS"] = str(args.embedding_dimensions)
        self.pipeline_steps: list[dict[str, Any]] = []
        self.raw_debug: dict[str, Any] = {}

    def run(self) -> dict[str, Any]:
        started_at = utc_now()
        self.recovery_log.parent.mkdir(parents=True, exist_ok=True)
        self.recovery_log.write_text("", encoding="utf-8")
        zip_files = sorted(self.args.input.glob("*.zip"))

        import_summary = {}
        extraction_summary = {}
        database_summary = {}
        graph = {"entities": [], "hyperedges": []}
        review_items = []
        source_items = []
        questions: list[str] = []
        pska_results = []
        mcp_results = []
        fastreact_results = []

        reset = self.run_command("db_reset", ["python3", "-m", "pska_core.cli", "db-reset", "--name", database_name(self.args.database_url)])
        if reset["status"] == "passed":
            imported = self.run_command(
                "twitter_zip_import",
                [
                    "python3",
                    "-m",
                    "pska_core.cli",
                    "--database-url",
                    self.args.database_url,
                    "import-twitter-zips",
                    "--input",
                    str(self.args.input),
                    "--archive-root",
                    str(self.args.archive_root),
                    "--owner-user-id",
                    self.args.owner_user_id,
                    "--embedding-provider",
                    self.args.embedding_provider,
                    "--embedding-model",
                    self.args.embedding_model,
                    "--embedding-dimensions",
                    str(self.args.embedding_dimensions),
                ],
                parse_json=True,
                timeout=180,
            )
            import_summary = imported.get("json") or {}
            if imported["status"] == "passed":
                if self.args.embedding_provider not in {"", "disabled", "none", "off"}:
                    self.run_command(
                        "embedding_backfill",
                        [
                            "python3",
                            "-m",
                            "pska_core.cli",
                            "--database-url",
                            self.args.database_url,
                            "embed-backfill",
                            "--embedding-provider",
                            self.args.embedding_provider,
                            "--embedding-model",
                            self.args.embedding_model,
                            "--embedding-dimensions",
                            str(self.args.embedding_dimensions),
                        ],
                        parse_json=True,
                        timeout=900,
                    )
                extracted = self.run_command(
                    "llm_extraction",
                    [
                        "python3",
                        "-m",
                        "pska_core.cli",
                        "--database-url",
                        self.args.database_url,
                        "extract-all",
                        "--owner-user-id",
                        self.args.owner_user_id,
                    ],
                    parse_json=True,
                    timeout=600,
                )
                extraction_summary = extracted.get("json") or {}

        try:
            database_summary = self.database_counts()
            source_items = self.source_summaries()
            graph = self.graph_summary()
            review_items = self.review_item_summaries()
            questions = build_questions(source_items, graph["entities"])
        except Exception as exc:  # noqa: BLE001
            self.pipeline_steps.append(step_error("database_introspection", exc))

        if questions:
            api_process = None if self.args.only_fastreact else self.start_api()
            try:
                for question in questions:
                    if self.args.only_fastreact:
                        self.pipeline_steps.append(
                            {"name": f"pska_direct:{question[:30]}", "status": "skipped", "reason": "--only-fastreact"}
                        )
                        self.pipeline_steps.append(
                            {"name": f"mcp:{question[:30]}", "status": "skipped", "reason": "--only-fastreact"}
                        )
                    else:
                        pska_results.append(self.run_pska_question(question))
                        mcp_results.append(self.run_mcp_question(question))
                    fastreact_results.append(self.run_fastreact_question(question))
            finally:
                stop_process(api_process)
        else:
            self.pipeline_steps.append({"name": "questions", "status": "failed", "error": "No questions generated."})

        recovery_events = read_recovery_events(self.recovery_log)
        overall_status = "passed" if all(step["status"] in {"passed", "skipped"} for step in self.pipeline_steps) else "failed"
        report = {
            "run_metadata": {
                "started_at": started_at,
                "finished_at": utc_now(),
                "run_id": self.run_id,
                "history_dir": str(self.args.history_dir),
                "overall_status": overall_status,
                "input_dir": str(self.args.input),
                "zip_count": len(zip_files),
                "database_url": self.args.database_url,
                "output": str(self.args.output),
                "json_output": str(self.args.json_output),
                "embedding": {
                    "provider": self.args.embedding_provider,
                    "model": self.args.embedding_model,
                    "dimensions": self.args.embedding_dimensions,
                },
            },
            "pipeline_steps": self.pipeline_steps,
            "database_summary": database_summary,
            "import_summary": import_summary,
            "extraction_summary": extraction_summary,
            "graph": graph,
            "review_items": review_items,
            "source_items": source_items,
            "questions": questions,
            "pska_results": pska_results,
            "mcp_results": mcp_results,
            "fastreact_results": fastreact_results,
            "recovery_events": recovery_events,
            "raw_debug": self.raw_debug,
        }
        return scrub_secrets(report)

    def run_command(
        self,
        name: str,
        command: list[str],
        *,
        parse_json: bool = False,
        timeout: int = 120,
    ) -> dict[str, Any]:
        if self.args.skip_import and name in {"db_reset", "twitter_zip_import", "embedding_backfill", "llm_extraction"}:
            step = {"name": name, "status": "skipped", "reason": "--skip-import"}
            self.pipeline_steps.append(step)
            return step
        started = time.time()
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        step = {
            "name": name,
            "status": "passed" if result.returncode == 0 else "failed",
            "duration_seconds": round(time.time() - started, 2),
            "command": command,
            "returncode": result.returncode,
            "stdout_tail": tail(scrub_text(result.stdout), 6000),
            "stderr_tail": tail(scrub_text(result.stderr), 6000),
        }
        if parse_json and result.stdout.strip():
            try:
                step["json"] = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                step["status"] = "failed"
                step["json_error"] = str(exc)
        self.pipeline_steps.append(step)
        return step

    def database_counts(self) -> dict[str, int]:
        tables = ["source_items", "documents", "chunks", "entities", "hyperedges", "review_items"]
        with psycopg.connect(self.args.database_url, row_factory=dict_row) as conn:
            counts = {table: int(conn.execute(f"select count(*) as count from {table}").fetchone()["count"]) for table in tables}
            counts["embedded_chunks"] = int(conn.execute("select count(*) as count from chunks where embedding is not null").fetchone()["count"])
            return counts

    def source_summaries(self) -> list[dict[str, Any]]:
        with psycopg.connect(self.args.database_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                select source_item_id, source_channel, record_type, source_id, title, url, content_text, metadata, created_at
                from source_items
                order by created_at, source_item_id
                """
            ).fetchall()
        return [
            {
                "source_item_id": row["source_item_id"],
                "source_channel": row["source_channel"],
                "record_type": row["record_type"],
                "source_id": row["source_id"],
                "title": row["title"],
                "url": row["url"],
                "created_at": row["created_at"],
                "source_created_at": (row["metadata"] or {}).get("created_at"),
                "captured_at": (row["metadata"] or {}).get("captured_at"),
                "author": dict((row["metadata"] or {}).get("author") or {}),
                "snippet": compact(row["content_text"], 420),
                "raw_paths": dict((row["metadata"] or {}).get("raw_paths") or {}),
            }
            for row in rows
        ]

    def graph_summary(self) -> dict[str, Any]:
        with psycopg.connect(self.args.database_url, row_factory=dict_row) as conn:
            entities = conn.execute(
                "select entity_id, entity_type, label, visibility from entities order by created_at, entity_id"
            ).fetchall()
            edges = conn.execute(
                """
                select h.hyperedge_id, h.relation_type, h.directionality, h.evidence_text, h.confidence, h.source_refs,
                       coalesce(json_agg(json_build_object(
                           'entity_id', e.entity_id,
                           'label', e.label,
                           'entity_type', e.entity_type,
                           'role', m.role,
                           'ordinal', m.ordinal
                       ) order by m.ordinal) filter (where e.entity_id is not null), '[]') as members
                from hyperedges h
                left join hyperedge_members m on m.hyperedge_id = h.hyperedge_id
                left join entities e on e.entity_id = m.entity_id
                group by h.hyperedge_id
                order by h.created_at, h.hyperedge_id
                """
            ).fetchall()
        return {
            "entities": [dict(row) for row in entities],
            "hyperedges": [dict(row) for row in edges],
        }

    def review_item_summaries(self) -> list[dict[str, Any]]:
        with psycopg.connect(self.args.database_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                select review_item_id, owner_user_id, review_type, title, status, proposal, created_at
                from review_items
                order by created_at, review_item_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def run_pska_question(self, question: str) -> dict[str, Any]:
        result: dict[str, Any] = {"question": question}
        search = self.run_command(
            f"pska_cli_search:{question[:30]}",
            [
                "python3",
                "-m",
                "pska_core.cli",
                "--database-url",
                self.args.database_url,
                "search",
                "--query",
                question,
                "--user-id",
                self.args.owner_user_id,
                "--top-k",
                str(self.args.top_k),
                "--embedding-provider",
                self.args.embedding_provider,
                "--embedding-model",
                self.args.embedding_model,
                "--embedding-dimensions",
                str(self.args.embedding_dimensions),
            ],
            parse_json=True,
            timeout=120,
        )
        result["cli_search"] = search.get("json")
        agentic = self.run_command(
            f"agentic_service_search:{question[:30]}",
            [
                "python3",
                "-m",
                "pska_core.cli",
                "--database-url",
                self.args.database_url,
                "agentic-search",
                "--query",
                question,
                "--user-id",
                self.args.owner_user_id,
                "--embedding-provider",
                self.args.embedding_provider,
                "--embedding-model",
                self.args.embedding_model,
                "--embedding-dimensions",
                str(self.args.embedding_dimensions),
            ],
            parse_json=True,
            timeout=240,
        )
        result["agentic_search"] = agentic.get("json")
        result["http_agentic_search"] = self.http_agentic(question)
        return result

    def start_api(self) -> subprocess.Popen[str] | None:
        process = subprocess.Popen(
            [
                "python3",
                "-m",
                "pska_core.cli",
                "--database-url",
                self.args.database_url,
                "serve",
                "--port",
                str(self.args.api_port),
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{self.args.api_port}/health", timeout=0.5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                self.pipeline_steps.append({"name": "http_api_start", "status": "passed", "health": health})
                return process
            except Exception:
                time.sleep(0.1)
        self.pipeline_steps.append({"name": "http_api_start", "status": "failed", "error": "server did not start"})
        stop_process(process)
        return None

    def http_agentic(self, question: str) -> dict[str, Any]:
        started = time.time()
        try:
            request = Request(
                f"http://127.0.0.1:{self.args.api_port}/agentic-search",
                data=json.dumps({"query": question, "user_id": self.args.owner_user_id}).encode("utf-8"),
                headers={"content-type": "application/json"},
            )
            with urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {"status": "passed", "duration_seconds": round(time.time() - started, 2), "payload": payload}
        except Exception as exc:  # noqa: BLE001
            self.pipeline_steps.append(step_error("http_agentic_search", exc))
            return {"status": "failed", "error": str(exc)}

    def run_mcp_question(self, question: str) -> dict[str, Any]:
        started = time.time()
        process = subprocess.Popen(
            [str(ROOT.parent / "scripts" / "pska"), "mcp-server"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        responses: list[dict[str, Any]] = []
        try:
            assert process.stdin and process.stdout
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "pska_search",
                        "arguments": {"query": question, "user_id": self.args.owner_user_id, "top_k": self.args.top_k},
                    },
                },
            ]
            for request in requests:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                if "id" in request:
                    responses.append(json.loads(process.stdout.readline()))
            status = "passed" if all("result" in response for response in responses) else "failed"
            self.pipeline_steps.append({"name": f"mcp:{question[:30]}", "status": status, "duration_seconds": round(time.time() - started, 2)})
            return {"question": question, "status": status, "responses": responses}
        except Exception as exc:  # noqa: BLE001
            self.pipeline_steps.append(step_error("mcp_question", exc))
            return {"question": question, "status": "failed", "error": str(exc), "responses": responses}
        finally:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            stop_process(process)

    def run_fastreact_question(self, question: str) -> dict[str, Any]:
        if self.args.fastreact_mode in {"auto", "api"}:
            api_result = self.run_fastreact_api_question(question, required=self.args.fastreact_mode == "api")
            if api_result["status"] == "passed" or self.args.fastreact_mode == "api":
                return api_result
            self.pipeline_steps.append(
                {
                    "name": f"fastreact_api_fallback:{question[:30]}",
                    "status": "skipped",
                    "reason": api_result.get("error", "Fastreact API unavailable"),
                }
            )
        return self.run_fastreact_local_question(question)

    def run_fastreact_api_question(self, question: str, *, required: bool) -> dict[str, Any]:
        started = time.time()
        try:
            payload = self._fastreact_api_payload(question)
            request = Request(
                f"{self.args.fastreact_api_url.rstrip('/')}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=self._fastreact_api_headers(),
            )
            with urlopen(request, timeout=self.args.fastreact_timeout) as response:
                events = parse_sse_events(response.read().decode("utf-8"))
            agent_answer = fastreact_agent_answer({"events": events})
            normalized_payload = {"events": events, "agent_answer": agent_answer, "transport": "api"}
            status = "passed" if agent_answer else "failed" if required else "skipped"
            self.pipeline_steps.append(
                {
                    "name": f"fastreact_api:{question[:30]}",
                    "status": status,
                    "duration_seconds": round(time.time() - started, 2),
                    "endpoint": "/v1/chat/completions",
                }
            )
            return {
                "question": question,
                "status": status,
                "transport": "api",
                "direct_mcp_status": "unknown",
                "full_agent_status": "passed" if agent_answer else "failed",
                "payload": normalized_payload,
            }
        except Exception as exc:  # noqa: BLE001
            status = "failed" if required else "skipped"
            self.pipeline_steps.append(
                {
                    "name": f"fastreact_api:{question[:30]}",
                    "status": status,
                    "duration_seconds": round(time.time() - started, 2),
                    "error": str(exc),
                }
            )
            return {"question": question, "status": status, "transport": "api", "error": str(exc), "payload": {}}

    def _fastreact_api_headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        token = (self.args.fastreact_service_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-FastReAct-Service-Token"] = token
        return headers

    def _fastreact_api_payload(self, question: str) -> dict[str, Any]:
        prompt = (
            "You are running as the Fastreact agentic service layer for PSKA. "
            "Use the configured PSKA MCP tools when available, cite evidence, and return the final answer."
        )
        payload = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            "stream": True,
            "user_key": f"pska:{self.args.owner_user_id}",
            "caller": "pska",
            "run_id": self.run_id,
            "purpose": "report",
            "pska_user_id": self.args.owner_user_id,
            "metadata": {
                "caller": "pska",
                "run_id": self.run_id,
                "purpose": "report",
                "pska_user_id": self.args.owner_user_id,
            },
        }
        if self.args.fastreact_model:
            payload["model"] = self.args.fastreact_model
        return payload

    def run_fastreact_local_question(self, question: str) -> dict[str, Any]:
        fastreact_src = resolve_fastreact_src()
        if not fastreact_src.exists():
            result = {"question": question, "status": "failed", "error": f"Fastreact source not found: {fastreact_src}"}
            self.pipeline_steps.append({"name": f"fastreact:{question[:30]}", **result})
            return result
        fast_env = self.env.copy()
        fast_env["PYTHONPATH"] = f"{SRC}:{fastreact_src}"
        fast_env["FASTRACT_MCP_SERVERS"] = json.dumps(
            [{"name": "pska", "command": str(ROOT.parent / "scripts" / "pska"), "args": ["mcp-server"], "isolation": "shared"}]
        )
        apply_fastreact_llm_env(fast_env)
        snippet = FASTREACT_SNIPPET.replace("__QUESTION_JSON__", json.dumps(question))
        started = time.time()
        try:
            result = subprocess.run(
                ["python3", "-c", snippet],
                cwd=ROOT,
                env=fast_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.args.fastreact_timeout,
            )
            payload = parse_json_from_stdout(result.stdout)
            status = "passed" if fastreact_payload_passed(result.returncode, payload) else "failed"
            self.pipeline_steps.append({
                "name": f"fastreact:{question[:30]}",
                "status": status,
                "duration_seconds": round(time.time() - started, 2),
                "stdout_tail": tail(scrub_text(result.stdout), 3000),
                "stderr_tail": tail(scrub_text(result.stderr), 3000),
            })
            return {
                "question": question,
                "status": status,
                "direct_mcp_status": "passed" if fastreact_direct_answer(payload) else "failed",
                "full_agent_status": "passed" if fastreact_agent_answer(payload) else "failed",
                "payload": payload,
                "stdout_tail": tail(scrub_text(result.stdout), 3000),
                "stderr_tail": tail(scrub_text(result.stderr), 3000),
            }
        except Exception as exc:  # noqa: BLE001
            self.pipeline_steps.append(step_error("fastreact_question", exc))
            return {"question": question, "status": "failed", "error": str(exc)}


FASTREACT_SNIPPET = r'''
import asyncio
import json
from fastreact import Agent
from fastreact.core.config import Config

QUESTION = __QUESTION_JSON__

async def main():
    agent = Agent(config=Config.from_env())
    events = []
    try:
        await agent._load_mcp_servers()
        tools = [name for name in agent._tools.list_all() if name.startswith("pska_")]
        search_text = await agent._tools.get("pska_pska_search").execute(query=QUESTION, user_id="user_primary", top_k=5)
        prompt = (
            "You must answer using PSKA MCP tools, not model-only knowledge. "
            "Question: " + QUESTION + "\n"
            "Use pska_pska_search and cite evidence. "
            "If evidence is insufficient, say insufficient evidence."
        )
        try:
            final_chunks = []
            async for event in agent.run_event_stream(prompt):
                events.append({
                    "type": str(event.type),
                    "content": event.content[:1200] if event.content else "",
                    "tool_name": event.tool_name,
                    "tool_args": event.tool_args,
                    "metadata": event.metadata,
                })
                if str(event.type).endswith("THINK") and event.content:
                    final_chunks.append(event.content)
                if str(event.type).endswith("ERROR"):
                    final_chunks.append("[FASTREACT_ERROR] " + event.content)
                if str(event.type).endswith("SESSION_END") and event.content:
                    final_chunks = [event.content]
            answer = "".join(final_chunks).strip()
        except Exception as exc:
            answer = "[FASTREACT_AGENT_FAILED] " + type(exc).__name__ + ": " + str(exc)
        if agent._mcp_manager:
            await agent._mcp_manager.close_all()
        print(json.dumps({
            "tools": tools,
            "direct_search": json.loads(search_text),
            "agent_answer": answer,
            "events": events,
        }, ensure_ascii=False))
    finally:
        if agent._mcp_manager:
            await agent._mcp_manager.close_all()

asyncio.run(main())
'''


def make_env(database_url: str, recovery_log: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["PSKA_DATABASE_URL"] = database_url
    env["PSKA_LLM_RECOVERY_LOG"] = str(recovery_log)
    key_file = Path.home() / "api_key.txt"
    if key_file.exists():
        env.setdefault("PSKA_LLM_API_KEY_FILE", str(key_file))
        key, model, base_url = read_api_key_file(key_file)
        if key:
            env.setdefault("FASTRACT_API_KEY", key)
            env.setdefault("OPENAI_API_KEY", key)
        if model:
            env.setdefault("FASTRACT_MODEL", model)
        if base_url:
            env.setdefault("FASTRACT_API_BASE", base_url)
    return env


def apply_fastreact_llm_env(env: dict[str, str]) -> None:
    key_file = Path(env.get("PSKA_LLM_API_KEY_FILE") or Path.home() / "api_key.txt")
    key, model, base_url = read_api_key_file(key_file)
    if key:
        env["FASTRACT_API_KEY"] = key
        env.setdefault("OPENAI_API_KEY", key)
    if model:
        env["FASTRACT_MODEL"] = model
    if base_url:
        env["FASTRACT_API_BASE"] = base_url


def build_questions(source_items: list[dict[str, Any]], entities: list[dict[str, Any]]) -> list[str]:
    text = " ".join([item.get("title") or "" for item in source_items] + [item.get("snippet") or "" for item in source_items])
    terms = [term for term in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", text) if len(term) >= 2]
    stop = {"https", "com", "status", "the", "and", "for", "with", "this", "that", "一个", "可以", "使用", "工具"}
    candidates = [term for term, _ in Counter(term for term in terms if term.lower() not in stop).most_common(8)]
    entity_labels = [str(entity.get("label")) for entity in entities[:8] if entity.get("label")]
    automatic = []
    for term in [*entity_labels, *candidates]:
        if term and all(term not in question for question in automatic):
            automatic.append(f"归档中关于 {term} 的关键信息是什么？")
        if len(automatic) >= 3:
            break
    questions = []
    for question in [*automatic, *FIXED_QUESTIONS]:
        if question not in questions:
            questions.append(question)
    return questions


def write_outputs(report: dict[str, Any], output: Path, json_output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    report.setdefault("technical_paths", default_technical_paths())
    report.setdefault("acceptance_checks", derive_acceptance_checks(report))
    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    html_text = render_html_report(report)
    json_output.write_text(json_text, encoding="utf-8")
    output.write_text(html_text, encoding="utf-8")

    meta = report.get("run_metadata") or {}
    run_id = meta.get("run_id")
    history_dir = meta.get("history_dir")
    if run_id and history_dir:
        run_dir = Path(str(history_dir)) / safe_filename(str(run_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(json_text, encoding="utf-8")
        (run_dir / "report.html").write_text(html_text, encoding="utf-8")


def parse_json_from_stdout(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No JSON object found in stdout", text, 0)


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        event_name = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
        data_lines = [line.removeprefix("data:").strip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            event = {"type": "invalid_sse_json", "content": data}
        if isinstance(event, dict):
            event.setdefault("schema", "fastreact.agent_event.v1")
            if event_name and "type" not in event:
                event["type"] = event_name
            events.append(event)
    return events


def fastreact_payload_passed(returncode: int, payload: dict[str, Any]) -> bool:
    return returncode == 0 and bool(fastreact_direct_answer(payload)) and bool(fastreact_agent_answer(payload))


def fastreact_direct_answer(payload: dict[str, Any]) -> str:
    answer = ((payload.get("agentic_search") or {}).get("answer") or "").strip()
    if answer:
        return answer
    search = payload.get("direct_search") or {}
    results = search.get("results") or []
    if results:
        return str(results[0].get("snippet") or "").strip()
    return ""


def fastreact_agent_answer(payload: dict[str, Any]) -> str:
    answer = str(payload.get("agent_answer") or "").strip()
    if answer:
        return answer
    for event in reversed(payload.get("events") or []):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").lower()
        content = str(event.get("content") or "").strip()
        if content and ("session_end" in event_type or "final_answer" in event_type or event_type.endswith("think")):
            return content
    return ""


def fastreact_event_stream(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not payload:
        return []

    events: list[dict[str, Any]] = []
    for name, key in (("pska_pska_search", "direct_search"),):
        value = payload.get(key)
        if value:
            events.append({"kind": "tool_call", "tool_name": name, "summary": "Fastreact direct MCP call"})
            events.append({"kind": "tool_result", "tool_name": name, "summary": compact(json.dumps(value, ensure_ascii=False), 500)})

    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        event_type_lower = event_type.lower()
        tool_name = event.get("tool_name")
        content = str(event.get("content") or "").strip()
        if tool_name and event.get("tool_args"):
            kind = "tool_call"
        elif tool_name:
            kind = "tool_result"
        elif "session_end" in event_type_lower or "final_answer" in event_type_lower:
            kind = "final_answer"
        elif "error" in event_type_lower:
            kind = "error"
        else:
            kind = "agent_event"
        events.append(
            {
                "kind": kind,
                "type": event_type,
                "tool_name": tool_name,
                "tool_args": event.get("tool_args"),
                "summary": compact(content or json.dumps(event.get("metadata") or {}, ensure_ascii=False), 500),
            }
        )

    agent_answer = fastreact_agent_answer(payload)
    if agent_answer and not any(event.get("kind") == "final_answer" for event in events):
        events.append({"kind": "final_answer", "summary": agent_answer})
    return events


def fastreact_event_stream_section(payload: dict[str, Any]) -> str:
    events = fastreact_event_stream(payload)
    if not events:
        return "<p>No Fastreact event stream captured.</p>"
    rows = "".join(
        "<tr>"
        f"<td>{index}</td>"
        f"<td>{esc(event.get('kind'))}</td>"
        f"<td>{esc(event.get('tool_name') or event.get('type'))}</td>"
        f"<td>{esc(event.get('summary'))}</td>"
        "</tr>"
        for index, event in enumerate(events, start=1)
    )
    return (
        "<h4>Fastreact Event Stream</h4>"
        "<table><thead><tr><th>#</th><th>Event</th><th>Tool/Type</th><th>Summary</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def technical_paths_section(report: dict[str, Any]) -> str:
    paths = report.get("technical_paths") or default_technical_paths()
    rows = "".join(
        "<tr>"
        f"<td>{esc(path.get('name'))}</td>"
        f"<td>{esc(path.get('entrypoint'))}</td>"
        f"<td>{esc(path.get('purpose'))}</td>"
        f"<td>{esc(path.get('difference'))}</td>"
        "</tr>"
        for path in paths
    )
    return section(
        "Technical Paths",
        "<table><thead><tr><th>Path</th><th>Entrypoint</th><th>Purpose</th><th>Difference</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>",
    )


def acceptance_section(report: dict[str, Any]) -> str:
    checks = report.get("acceptance_checks") or derive_acceptance_checks(report)
    rows = "".join(
        "<tr>"
        f"<td>{esc(check.get('name'))}</td>"
        f"<td class='{esc(check.get('status'))}'>{esc(check.get('status'))}</td>"
        f"<td>{esc(check.get('detail'))}</td>"
        "</tr>"
        for check in checks
    )
    return section(
        "Acceptance Checks",
        "<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>",
    )


def bottleneck_section(report: dict[str, Any], *, limit: int = 5) -> str:
    steps = report.get("pipeline_steps") or report.get("run_metadata", {}).get("pipeline_steps", [])
    timed_steps = [
        step
        for step in steps
        if isinstance(step.get("duration_seconds"), (int, float)) and step.get("duration_seconds", 0) > 0
    ]
    if not timed_steps:
        return section("Duration Bottlenecks", "<p>No timed pipeline steps recorded.</p>")
    rows = "".join(
        "<tr>"
        f"<td>{index}</td>"
        f"<td>{esc(step.get('name'))}</td>"
        f"<td>{esc(step.get('status'))}</td>"
        f"<td>{esc(step.get('duration_seconds'))}</td>"
        "</tr>"
        for index, step in enumerate(sorted(timed_steps, key=lambda item: item.get("duration_seconds", 0), reverse=True)[:limit], start=1)
    )
    return section(
        "Duration Bottlenecks",
        "<table><thead><tr><th>#</th><th>Step</th><th>Status</th><th>Seconds</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>",
    )


def review_section(report: dict[str, Any]) -> str:
    reviews = report.get("review_items") or []
    if not reviews:
        return section("Review Items", "<p>No review items found.</p>")
    rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('review_item_id'))}</td>"
        f"<td>{esc(item.get('review_type'))}</td>"
        f"<td class='{esc(item.get('status'))}'>{esc(item.get('status'))}</td>"
        f"<td>{esc(item.get('title'))}</td>"
        "</tr>"
        for item in reviews
    )
    return section(
        "Review Items",
        "<table><thead><tr><th>ID</th><th>Type</th><th>Status</th><th>Title</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>",
    )


def default_technical_paths() -> list[dict[str, str]]:
    return [
        {
            "name": "PSKA direct",
            "entrypoint": "pska_core.cli agentic-search + HTTP /agentic-search",
            "purpose": "Validate local retrieval, ACL, embedding, hypergraph, and answer synthesis.",
            "difference": "Runs PSKA directly without MCP agent orchestration.",
        },
        {
            "name": "MCP direct",
            "entrypoint": "pska mcp-server tools/call",
            "purpose": "Validate the MCP protocol surface used by external agents.",
            "difference": "Exercises tool schemas and JSON-RPC transport.",
        },
        {
            "name": "Fastreact full Agent",
            "entrypoint": "Fastreact HTTP /v1/chat/completions SSE with PSKA MCP server configured by Fastreact",
            "purpose": "Validate external agent service planning, tool calls, tool results, and final answer.",
            "difference": "Uses Fastreact as an agentic service boundary; local import is only a fallback for offline reports.",
        },
    ]


def derive_acceptance_checks(report: dict[str, Any]) -> list[dict[str, str]]:
    steps = report.get("run_metadata", {}).get("pipeline_steps", [])
    failed_steps = [step.get("name", "unknown") for step in steps if step.get("status") == "failed"]
    skipped_steps = [step.get("name", "unknown") for step in steps if step.get("status") == "skipped"]
    recovery_events = report.get("recovery_events") or []
    db = report.get("database_summary") or {}
    checks = [
        {
            "name": "Artifacts generated",
            "status": "passed",
            "detail": "HTML and JSON report writers run even when individual stages fail.",
        },
        {
            "name": "Failure visibility",
            "status": "failed" if failed_steps else "passed",
            "detail": ", ".join(failed_steps) if failed_steps else "No failed pipeline steps.",
        },
        {
            "name": "Stage selection",
            "status": "skipped" if skipped_steps else "passed",
            "detail": ", ".join(skipped_steps) if skipped_steps else "All configured stages ran.",
        },
        {
            "name": "LLM/schema repair visibility",
            "status": "passed" if recovery_events else "skipped",
            "detail": f"{len(recovery_events)} recovery event(s) captured.",
        },
        {
            "name": "Embedding status",
            "status": "passed" if int(db.get("embedded_chunks") or 0) else "skipped",
            "detail": f"{db.get('embedded_chunks', 0)} embedded chunk(s).",
        },
    ]
    return checks


def render_html_report(report: dict[str, Any]) -> str:
    meta = report["run_metadata"]
    db = report.get("database_summary") or {}
    graph = report.get("graph") or {"entities": [], "hyperedges": []}
    fastreact_ok = any((item.get("direct_mcp_status") == "passed" or item.get("status") == "passed") for item in report.get("fastreact_results", []))
    cards = [
        ("Overall", meta.get("overall_status")),
        ("ZIP files", meta.get("zip_count")),
        ("Sources", db.get("source_items", 0)),
        ("Embedded chunks", db.get("embedded_chunks", 0)),
        ("Entities", db.get("entities", 0)),
        ("Hyperedges", db.get("hyperedges", 0)),
        ("Questions", len(report.get("questions", []))),
        ("Fastreact", "passed" if fastreact_ok else "failed"),
    ]
    html_parts = [
        "<!doctype html><html><head><meta charset='utf-8'><title>PSKA Twitter Full Test</title>",
        "<style>",
        CSS,
        "</style></head><body>",
        f"<header><h1>PSKA + Fastreact Twitter Archive Full Test</h1><p>{esc(meta.get('started_at'))} - {esc(meta.get('finished_at'))}</p></header>",
        "<section class='cards'>" + "".join(card(label, value) for label, value in cards) + "</section>",
        technical_paths_section(report),
        section("Pipeline", pipeline_table(report.get("pipeline_steps", []))),
        bottleneck_section(report),
        acceptance_section(report),
        section("Data", data_section(report)),
        section("Provenance", provenance_section(report)),
        section("Knowledge Graph", graph_section(graph)),
        review_section(report),
        section("Question Answering", qa_section(report)),
        section("Recovery / Fallback", recovery_section(report)),
        section("Raw Evidence", details_json("Full JSON report", report)),
        "</body></html>",
    ]
    return "\n".join(html_parts)


CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#1c2430}
header{padding:28px 36px;background:#17202f;color:#fff} h1{margin:0 0 8px}
section{margin:22px 36px;padding:20px;background:#fff;border:1px solid #dde2ea;border-radius:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;background:transparent;border:0;padding:0}
.card{background:#fff;border:1px solid #dde2ea;border-radius:8px;padding:14px}.card b{display:block;font-size:22px;margin-top:4px}
.passed{color:#137333}.failed{color:#b3261e}.warning{color:#9a6700}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #e6e9ef;padding:8px;text-align:left;vertical-align:top}
pre{white-space:pre-wrap;word-break:break-word;background:#f4f6f8;padding:12px;border-radius:6px;max-height:520px;overflow:auto}
details{margin:8px 0}.qcard{border:1px solid #e1e5ec;border-radius:8px;padding:14px;margin:12px 0}
.graph{width:100%;height:auto;border:1px solid #e1e5ec;background:#fbfcfe;border-radius:8px}.pill{display:inline-block;padding:2px 7px;border-radius:999px;background:#eef2f7;margin:2px}
"""


def card(label: str, value: Any) -> str:
    cls = "failed" if str(value) == "failed" else "passed" if str(value) == "passed" else ""
    return f"<div class='card'><span>{esc(label)}</span><b class='{cls}'>{esc(value)}</b></div>"


def section(title: str, body: str) -> str:
    return f"<section><h2>{esc(title)}</h2>{body}</section>"


def pipeline_table(steps: list[dict[str, Any]]) -> str:
    rows = []
    for step in steps:
        rows.append(
            "<tr>"
            f"<td>{esc(step.get('name'))}</td>"
            f"<td class='{esc(step.get('status'))}'>{esc(step.get('status'))}</td>"
            f"<td>{esc(step.get('duration_seconds', ''))}</td>"
            f"<td>{esc(step.get('error') or step.get('stderr_tail') or '')}</td>"
            "</tr>"
        )
    return "<table><tr><th>Step</th><th>Status</th><th>Seconds</th><th>Notes</th></tr>" + "".join(rows) + "</table>"


def data_section(report: dict[str, Any]) -> str:
    rows = []
    for item in report.get("source_items", []):
        rows.append(
            "<tr>"
            f"<td>{esc(item.get('source_id'))}</td><td>{esc(item.get('title'))}</td>"
            f"<td>{link(item.get('url'))}</td><td>{esc(item.get('snippet'))}</td>"
            "</tr>"
        )
    return (
        "<h3>Database summary</h3>"
        + details_json("Counts", report.get("database_summary"))
        + "<h3>Source items</h3><table><tr><th>Source ID</th><th>Title</th><th>URL</th><th>Snippet</th></tr>"
        + "".join(rows)
        + "</table>"
        + details_json("Import summary", report.get("import_summary"))
        + details_json("Extraction summary", report.get("extraction_summary"))
    )


def provenance_section(report: dict[str, Any]) -> str:
    source_rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('source_id'))}</td>"
        f"<td>{esc(item.get('source_channel'))}/{esc(item.get('record_type'))}</td>"
        f"<td>{esc((item.get('author') or {}).get('handle') or (item.get('author') or {}).get('name'))}</td>"
        f"<td>{esc(item.get('source_created_at') or item.get('created_at'))}</td>"
        f"<td>{esc(item.get('captured_at'))}</td>"
        f"<td>{link(item.get('url'))}</td>"
        "</tr>"
        for item in report.get("source_items", [])[:120]
    )
    edge_rows = "".join(
        "<tr>"
        f"<td>{esc(edge.get('relation_type'))}</td>"
        f"<td>{esc(edge.get('evidence_text'))}</td>"
        f"<td>{details_json('source_refs', edge.get('source_refs') or [])}</td>"
        "</tr>"
        for edge in (report.get("graph") or {}).get("hyperedges", [])[:120]
    )
    return (
        "<h3>Source provenance</h3>"
        "<table><tr><th>Source ID</th><th>Channel/type</th><th>Participant</th><th>Source time</th><th>Captured</th><th>URL</th></tr>"
        + source_rows
        + "</table><h3>Extraction evidence</h3>"
        + "<table><tr><th>Relation</th><th>Evidence</th><th>Source refs</th></tr>"
        + edge_rows
        + "</table>"
    )


def graph_section(graph: dict[str, Any]) -> str:
    entities = graph.get("entities", [])
    edges = graph.get("hyperedges", [])
    entity_rows = "".join(
        f"<tr><td>{esc(entity.get('label'))}</td><td>{esc(entity.get('entity_type'))}</td><td>{esc(entity.get('visibility'))}</td></tr>"
        for entity in entities[:80]
    )
    edge_rows = "".join(
        f"<tr><td>{esc(edge.get('relation_type'))}</td><td>{member_pills(edge.get('members') or [])}</td><td>{esc(edge.get('evidence_text'))}</td></tr>"
        for edge in edges[:80]
    )
    return (
        render_svg_graph(entities[:18], edges[:24])
        + "<h3>Entities</h3><table><tr><th>Label</th><th>Type</th><th>Visibility</th></tr>"
        + entity_rows
        + "</table><h3>Hyperedges</h3><table><tr><th>Relation</th><th>Members</th><th>Evidence</th></tr>"
        + edge_rows
        + "</table>"
    )


def render_svg_graph(entities: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    if not entities:
        return "<p>No graph entities extracted.</p>"
    width, height = 920, 420
    cx, cy, radius = width / 2, height / 2, 160
    positions = {}
    for index, entity in enumerate(entities):
        angle = (index / max(1, len(entities))) * 6.28318
        positions[entity["entity_id"]] = (cx + radius * __import__("math").cos(angle), cy + radius * __import__("math").sin(angle))
    lines = []
    for edge in edges:
        members = [member.get("entity_id") for member in edge.get("members") or [] if member.get("entity_id") in positions]
        for a, b in zip(members, members[1:]):
            x1, y1 = positions[a]
            x2, y2 = positions[b]
            lines.append(f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' stroke='#9aa7b8'/>")
    nodes = []
    for entity in entities:
        x, y = positions[entity["entity_id"]]
        nodes.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='18' fill='#dbeafe' stroke='#2563eb'/><text x='{x+22:.1f}' y='{y+4:.1f}' font-size='11'>{esc(entity.get('label'))}</text>")
    return f"<svg class='graph' viewBox='0 0 {width} {height}'>{''.join(lines)}{''.join(nodes)}</svg>"


def qa_section(report: dict[str, Any]) -> str:
    by_question = {item["question"]: item for item in report.get("pska_results", [])}
    fast_by_question = {item["question"]: item for item in report.get("fastreact_results", [])}
    mcp_by_question = {item["question"]: item for item in report.get("mcp_results", [])}
    cards = []
    for question in report.get("questions", []):
        pska = by_question.get(question, {})
        agentic = pska.get("agentic_search") or {}
        fast = fast_by_question.get(question, {})
        fast_payload = fast.get("payload") or {}
        direct_answer = fastreact_direct_answer(fast_payload)
        agent_answer = fastreact_agent_answer(fast_payload)
        mcp = mcp_by_question.get(question, {})
        cards.append(
            "<div class='qcard'>"
            f"<h3>{esc(question)}</h3>"
            f"<p><b>PSKA answer:</b> {esc(agentic.get('answer') or 'No answer')}</p>"
            f"<p><b>Fastreact MCP direct answer:</b> {esc(direct_answer or fast.get('error') or 'No direct MCP answer')}</p>"
            f"<p><b>Fastreact full Agent answer:</b> {esc(agent_answer or 'Full Agent returned an empty answer')}</p>"
            f"<p><b>Fastreact statuses:</b> direct MCP = {esc(fast.get('direct_mcp_status') or 'unknown')}; full Agent = {esc(fast.get('full_agent_status') or 'unknown')}</p>"
            + details_json("PSKA agentic trace", (agentic.get("trace") or {}))
            + details_json("Citations", ((agentic.get("retrieval") or {}).get("citations") or []))
            + details_json("Hypergraph context", ((agentic.get("retrieval") or {}).get("hypergraph_context") or []))
            + details_json("MCP responses", mcp)
            + fastreact_event_stream_section(fast_payload)
            + details_json("Fastreact raw", fast)
            + "</div>"
        )
    return "".join(cards)


def recovery_section(report: dict[str, Any]) -> str:
    events = report.get("recovery_events", [])
    forbidden = [event for event in events if "rule" in str(event).lower() or "fallback" in str(event).lower()]
    if not events:
        body = "<p class='passed'>No LLM repair events recorded. No rule-based fallback detected.</p>"
    else:
        body = "<p class='warning'>LLM repair events were used. These are schema/JSON repairs, not rule-based fallback.</p>"
        body += details_json("Recovery events", events)
    if forbidden:
        body += "<p class='failed'>Potential forbidden fallback marker detected.</p>"
    else:
        body += "<p class='passed'>No forbidden rule-based fallback marker detected.</p>"
    return body


def details_json(label: str, value: Any) -> str:
    return f"<details><summary>{esc(label)}</summary><pre>{esc(json.dumps(to_jsonable(value), ensure_ascii=False, indent=2))}</pre></details>"


def member_pills(members: list[dict[str, Any]]) -> str:
    return " ".join(f"<span class='pill'>{esc(member.get('role'))}: {esc(member.get('label'))}</span>" for member in members)


def link(url: str | None) -> str:
    if not url:
        return ""
    return f"<a href='{esc(url)}'>{esc(url)}</a>"


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def read_recovery_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"kind": "invalid_recovery_log_line", "raw": line})
    return events


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {key: scrub_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_secrets(item) for item in value]
    if is_dataclass(value):
        return scrub_secrets(asdict(value))
    if isinstance(value, Path):
        return scrub_text(str(value))
    return value


def scrub_text(text: Any) -> str:
    value = "" if text is None else str(text)
    secrets = [os.getenv("PSKA_LLM_API_KEY"), os.getenv("OPENAI_API_KEY"), os.getenv("FASTRACT_API_KEY"), read_first_line(Path.home() / "api_key.txt")]
    for secret in [item for item in secrets if item]:
        value = value.replace(secret, "***")
    value = value.replace(str(Path.home()), "~")
    return value


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def read_first_line(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except OSError:
        return ""
    return ""


def resolve_fastreact_src() -> Path:
    configured = os.environ.get("FASTREACT_SRC")
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


def read_api_key_file(path: Path) -> tuple[str, str, str]:
    key_config = read_pska_api_key_file(path)
    return key_config.api_key, key_config.model, key_config.base_url


def database_name(database_url: str) -> str:
    match = re.search(r"/([^/?]+)(?:[?].*)?$", database_url)
    return match.group(1) if match else "pska_smoke"


def tail(text: str, limit: int) -> str:
    return text[-limit:] if len(text) > limit else text


def compact(text: str, limit: int) -> str:
    return tail(re.sub(r"\s+", " ", text or "").strip(), limit)


def make_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{timestamp}"


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "run"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def step_error(name: str, exc: Exception) -> dict[str, Any]:
    return {"name": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if not process:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
