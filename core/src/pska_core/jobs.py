from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from pska_core.candidates import CandidateWriteService
from pska_core.config import WorkspaceConfig
from pska_core.embeddings import EmbeddingConfig, EmbeddingProvider, EmbeddingService, build_embedding_provider
from pska_core.enums import Visibility
from pska_core.extraction import ExtractionService
from pska_core.fastreact_client import FastreactClient, HttpFastreactClient
from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.models import Job
from pska_core.serde import to_jsonable
from pska_core.llm import LLMClient
from pska_core.store_postgres import PostgresKnowledgeStore

IMPORT_TWITTER_ZIPS = "import_twitter_zips"
EXTRACT_ALL = "extract_all"
EXTRACT_VIA_FASTREACT = "extract_via_fastreact"
DIGEST_VIA_FASTREACT = "digest_via_fastreact"
EMBED_BACKFILL = "embed_backfill"
REVIEW_APPLY = "review_apply"
FULL_REPORT = "full_report"

JOB_TYPES = {
    IMPORT_TWITTER_ZIPS,
    EXTRACT_ALL,
    EXTRACT_VIA_FASTREACT,
    DIGEST_VIA_FASTREACT,
    EMBED_BACKFILL,
    REVIEW_APPLY,
    FULL_REPORT,
}


def _default_workspace_root() -> Path:
    return WorkspaceConfig().root


@dataclass(slots=True)
class JobRunReport:
    processed: int
    succeeded: int
    failed: int
    jobs: list[Job]


class JobService:
    def __init__(
        self,
        store: PostgresKnowledgeStore,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        llm: LLMClient | None = None,
        fastreact: FastreactClient | None = None,
        workspace_root: Path | None = None,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        excluded_job_types: set[str] | None = None,
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider
        self.llm = llm
        self.fastreact = fastreact
        self.workspace_root = workspace_root or _default_workspace_root()
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.excluded_job_types = excluded_job_types or set()

    def submit(self, job_type: str, payload: dict[str, Any] | None = None, *, max_attempts: int = 3, priority: int = 0) -> Job:
        if job_type not in JOB_TYPES:
            raise ValueError(f"Unsupported job type: {job_type}")
        payload = dict(payload or {})
        if "priority" in payload and priority == 0:
            priority = int(payload["priority"])
        return self.store.create_job(job_type, payload, max_attempts=max_attempts, priority=priority)

    def run_next(self) -> Job | None:
        job = self.store.claim_next_job(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            excluded_job_types=self.excluded_job_types,
        )
        if job is None:
            return None
        try:
            result = self._execute(job)
        except Exception as exc:  # noqa: BLE001 - job failures must be recorded, not raised.
            return self.store.fail_job(job.job_id, f"{type(exc).__name__}: {exc}")
        return self.store.finish_job(job.job_id, result)

    def run_available(self, *, limit: int = 1) -> JobRunReport:
        jobs: list[Job] = []
        for _ in range(limit):
            job = self.run_next()
            if job is None:
                break
            jobs.append(job)
        return JobRunReport(
            processed=len(jobs),
            succeeded=sum(1 for job in jobs if job.status == "succeeded"),
            failed=sum(1 for job in jobs if job.status == "failed"),
            jobs=jobs,
        )

    def run_until_empty(self, *, limit: int | None = None) -> JobRunReport:
        jobs: list[Job] = []
        while limit is None or len(jobs) < limit:
            job = self.run_next()
            if job is None:
                break
            jobs.append(job)
        return JobRunReport(
            processed=len(jobs),
            succeeded=sum(1 for job in jobs if job.status == "succeeded"),
            failed=sum(1 for job in jobs if job.status == "failed"),
            jobs=jobs,
        )

    def recover_stale(self, *, max_age_seconds: int) -> list[Job]:
        return self.store.recover_stale_jobs(max_age_seconds=max_age_seconds)

    def _execute(self, job: Job) -> dict[str, Any]:
        self.store.add_job_event(job.job_id, "execute", f"Executing {job.job_type}")
        if job.job_type == IMPORT_TWITTER_ZIPS:
            return self._import_twitter_zips(job.payload)
        if job.job_type == EXTRACT_ALL:
            return self._extract_all(job.payload)
        if job.job_type == EXTRACT_VIA_FASTREACT:
            return self._extract_via_fastreact(job)
        if job.job_type == DIGEST_VIA_FASTREACT:
            return self._digest_via_fastreact(job)
        if job.job_type == EMBED_BACKFILL:
            return self._embed_backfill(job.payload)
        if job.job_type == REVIEW_APPLY:
            return self._review_apply(job.payload)
        if job.job_type == FULL_REPORT:
            return self._full_report(job)
        raise ValueError(f"Unsupported job type: {job.job_type}")

    def _import_twitter_zips(self, payload: dict[str, Any]) -> dict[str, Any]:
        embedding_provider = self._embedding_provider(payload)
        workspace_root = self.workspace_root
        importer = TwitterZipImporter(
            self.store,
            archive_root=Path(payload.get("archive_root") or workspace_root / "imports"),
            owner_user_id=str(payload.get("owner_user_id") or "user_primary"),
            space_id=str(payload.get("space_id") or "private_primary"),
            visibility=Visibility(payload.get("visibility") or Visibility.PRIVATE.value),
            visible_team_ids=_visible_team_ids(payload.get("visible_team_ids")),
            embedding_provider=embedding_provider,
        )
        result = importer.import_directory(Path(payload.get("input") or workspace_root / "twitter_archive"))
        return to_jsonable(result)

    def _extract_all(self, payload: dict[str, Any]) -> dict[str, Any]:
        reports = ExtractionService(self.store, llm=self.llm).extract_all_visible(owner_user_id=payload.get("owner_user_id"))
        return {"reports": to_jsonable(reports)}

    def _extract_via_fastreact(self, job: Job) -> dict[str, Any]:
        payload = job.payload
        owner_user_id = str(payload.get("owner_user_id") or "user_primary")
        top_k = int(payload.get("top_k") or 20)
        source_items = [
            {
                "source_item_id": item.source_item_id,
                "source_channel": item.source_channel,
                "record_type": item.record_type,
                "source_id": item.source_id,
                "title": item.title,
                "url": item.url,
                "content_text": item.content_text[:4000],
            }
            for item in self.store.list_source_items()
            if item.owner_user_id == owner_user_id
        ][:top_k]
        prompt = (
            "Run PSKA extraction for the provided source items. "
            "Use only PSKA MCP tools when available. Do not call host tools such as "
            "exec, shell, read_file, write_file, edit_file, or direct database clients. "
            "Return JSON-compatible candidate entities, hyperedges, review_items, "
            "cited_source_ids, and gaps. Do not invent facts beyond the provided source refs. "
            "If you need full source/chunk text, call pska_job_context with "
            f"job_id={job.job_id!r}, then write grounded candidates with pska_write_candidates. "
            "Every entity and every hyperedge member must include both entity_type and label. "
            "Every review item must include review_type, title, proposal, and source_refs. "
            "Call pska_write_candidates at most once. Keep the response compact and include "
            "candidate keys at the top level.\n\n"
            f"Source items:\n{to_jsonable(source_items)}"
        )
        response = self._call_fastreact(
            job=job,
            purpose="extract",
            user_id=owner_user_id,
            prompt=prompt,
            scope={"source_item_ids": [item["source_item_id"] for item in source_items]},
        )
        return {"fastreact": response, "candidate_write": self._write_fastreact_candidates(job, owner_user_id, response)}

    def _digest_via_fastreact(self, job: Job) -> dict[str, Any]:
        payload = job.payload
        owner_user_id = str(payload.get("owner_user_id") or "user_primary")
        scope = dict(payload.get("scope") or {})
        source_item_ids = [str(item) for item in scope.get("source_item_ids") or [] if item]
        prompt = (
            "Run one PSKA digest pass for the explicit job below. "
            f"job_id: {job.job_id}\n"
            f"owner_user_id: {owner_user_id}\n"
            f"source_item_ids: {source_item_ids}\n\n"
            "Use only PSKA MCP tools. Do not call host tools such as exec, shell, read_file, "
            "write_file, edit_file, or direct database clients.\n\n"
            "Required tool flow:\n"
            "1. Call pska_job_context with this job_id to fetch allowed source/chunk context.\n"
            "2. Produce only grounded candidates from that context.\n"
            "3. Call pska_write_candidates with schema_version='pska.candidates.v1', "
            "owner_user_id, source_refs, and any entities/hyperedges/review_items/memory_candidates.\n"
            "   Entity schema: {entity_type, label, confidence, source_refs}.\n"
            "   Hyperedge schema: {relation_type, members:[{entity_type,label,role}], evidence_text, confidence, source_refs}.\n"
            "   Review schema: {review_type, title, proposal, source_refs}.\n"
            "4. Call pska_write_candidates at most once.\n"
            "5. Return compact JSON with top-level candidate keys and source_refs. "
            "High-impact or low-confidence suggestions must be review candidates, not applied changes."
        )
        response = self._call_fastreact(
            job=job,
            purpose="digest",
            user_id=owner_user_id,
            prompt=prompt,
            scope=scope,
        )
        return {"fastreact": response, "candidate_write": self._write_fastreact_candidates(job, owner_user_id, response)}

    def _review_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        from pska_core.review import ReviewService

        review_item_id = str(payload["review_item_id"])
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "applied by review_apply job")
        review_item = ReviewService(self.store).apply(review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"review_item": to_jsonable(review_item)}

    def _call_fastreact(
        self,
        *,
        job: Job,
        purpose: str,
        user_id: str,
        prompt: str,
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        client = self.fastreact or HttpFastreactClient()
        response = client.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Fastreact executing a PSKA knowledge job. Use only PSKA MCP tools, "
                        "cite evidence, and never call host tools such as exec, shell, read_file, "
                        "write_file, edit_file, or direct database clients."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            user_id=user_id,
            purpose=purpose,
            stream=False,
            job_id=job.job_id,
            scope=scope,
        )
        run_id = response.get("run_id")
        self.store.add_job_event(
            job.job_id,
            "fastreact_submitted",
            "Submitted PSKA job to Fastreact",
            {"run_id": run_id, "purpose": purpose},
        )
        if run_id:
            self.store.heartbeat_job(
                job.job_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                external_run_id=str(run_id),
            )
        trace_summary = _fastreact_trace_summary(response)
        if trace_summary["event_count"]:
            self.store.add_job_event(job.job_id, "fastreact_trace", "Recorded Fastreact event summary", trace_summary)
        return to_jsonable(response)

    def _write_fastreact_candidates(self, job: Job, owner_user_id: str, response: dict[str, Any]) -> dict[str, Any]:
        candidate_keys = {"entities", "hyperedges", "review_items", "memory_candidates"}
        tool_errors = _fastreact_candidate_tool_errors(response)
        if tool_errors:
            detail = "; ".join(tool_errors[:3])
            raise RuntimeError(f"Fastreact candidate write tool failed: {detail}")
        if not candidate_keys.intersection(response.keys()):
            return {"skipped": True, "reason": "no_candidate_keys"}
        payload = {
            "schema_version": response.get("schema_version") or "pska.candidates.v1",
            "owner_user_id": owner_user_id,
            "job_id": job.job_id,
            "request_id": response.get("request_id") or response.get("run_id"),
            "producer": "fastreact",
            "source_refs": response.get("source_refs") or [to_jsonable(ref) for ref in job.source_refs],
            "entities": response.get("entities") or [],
            "hyperedges": response.get("hyperedges") or [],
            "review_items": response.get("review_items") or [],
            "memory_candidates": response.get("memory_candidates") or response.get("memory") or [],
        }
        summary = CandidateWriteService(self.store).write_candidates(payload)
        self.store.add_job_event(job.job_id, "candidates_written", "Wrote Fastreact candidates to PSKA", summary)
        return summary

    def _embed_backfill(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self._embedding_provider(payload)
        if provider is None:
            raise ValueError("embed_backfill job requires embedding_provider")
        report = EmbeddingService(
            self.store,
            provider,
            batch_size=int(payload.get("batch_size") or 16),
        ).backfill_missing(limit=int(payload["limit"]) if payload.get("limit") is not None else None)
        return to_jsonable(report)

    def _full_report(self, job: Job) -> dict[str, Any]:
        payload = job.payload
        core_root = Path(__file__).resolve().parents[2]
        script_path = Path(payload.get("script_path") or core_root / "scripts" / "twitter_full_report.py")
        timeout = int(payload.get("timeout") or 900)

        if payload.get("args") is not None:
            command = [sys.executable, str(script_path), *[str(item) for item in payload["args"]]]
            output_path = payload.get("output")
            json_output_path = payload.get("json_output")
        else:
            workspace_root = self.workspace_root
            output_path = str(payload.get("output") or core_root / "reports" / f"job_{job.job_id}.html")
            json_output_path = str(payload.get("json_output") or core_root / "reports" / f"job_{job.job_id}.json")
            command = [
                sys.executable,
                str(script_path),
                "--input",
                str(payload.get("input") or workspace_root / "twitter_archive"),
                "--database-url",
                str(payload.get("database_url") or getattr(self.store, "database_url", "postgresql:///pska_smoke")),
                "--archive-root",
                str(payload.get("archive_root") or workspace_root / "imports"),
                "--output",
                output_path,
                "--json-output",
                json_output_path,
                "--owner-user-id",
                str(payload.get("owner_user_id") or "user_primary"),
                "--top-k",
                str(payload.get("top_k") or 5),
                "--api-port",
                str(payload.get("api_port") or 8767),
                "--fastreact-timeout",
                str(payload.get("fastreact_timeout") or 180),
                "--embedding-provider",
                str(payload.get("embedding_provider") or "disabled"),
                "--embedding-model",
                str(payload.get("embedding_model") or "BAAI/bge-m3"),
                "--embedding-dimensions",
                str(payload.get("embedding_dimensions") or 1024),
            ]

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        if json_output_path:
            Path(json_output_path).parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["PYTHONPATH"] = f"{core_root / 'src'}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(core_root / "src")
        completed = subprocess.run(
            command,
            cwd=core_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        result = {
            "returncode": completed.returncode,
            "script": script_path.name,
            "output": _scrub_path(output_path),
            "json_output": _scrub_path(json_output_path),
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }
        if completed.returncode != 0:
            raise RuntimeError(f"full_report failed with exit code {completed.returncode}: {completed.stderr[-1000:]}")
        return result

    def _embedding_provider(self, payload: dict[str, Any]):
        if self.embedding_provider is not None:
            return self.embedding_provider
        config = EmbeddingConfig(
            provider=str(payload.get("embedding_provider") or "disabled"),
            model=str(payload.get("embedding_model") or "BAAI/bge-m3"),
            dimensions=int(payload.get("embedding_dimensions") or 1024),
            batch_size=int(payload.get("batch_size") or 16),
        )
        return build_embedding_provider(config)


def _visible_team_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise TypeError("visible_team_ids must be a list or comma-separated string")


def _fastreact_candidate_tool_errors(response: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for event in response.get("events") or []:
        if not isinstance(event, dict):
            continue
        tool_name = str(event.get("tool_name") or event.get("name") or "")
        content = str(event.get("content") or event.get("error") or "")
        if tool_name not in {"pska_write_candidates", "pska_pska_write_candidates"} and "pska_write_candidates" not in content:
            continue
        if "[MCP_ERROR]" in content or "[TOOL_BUDGET_DENIED]" in content:
            sequence = event.get("sequence")
            prefix = f"event {sequence}: " if sequence is not None else ""
            errors.append(f"{prefix}{content}")
    return errors


def _fastreact_trace_summary(response: dict[str, Any]) -> dict[str, Any]:
    events = [event for event in response.get("events") or [] if isinstance(event, dict)]
    tool_events = []
    for event in events:
        tool_name = str(event.get("tool_name") or event.get("name") or "")
        content = str(event.get("content") or event.get("error") or "")
        if not tool_name and "[TOOL_BUDGET_DENIED]" not in content and "[MCP_ERROR]" not in content:
            continue
        tool_events.append(
            {
                "sequence": event.get("sequence"),
                "type": event.get("type") or event.get("event"),
                "tool_name": tool_name or None,
                "content": content[:1000],
            }
        )
    return {
        "run_id": response.get("run_id"),
        "event_count": len(events),
        "tool_events": tool_events[-30:],
        "candidate_tool_errors": _fastreact_candidate_tool_errors(response),
    }


def _scrub_path(value: Any) -> str | None:
    if value is None:
        return None
    path = Path(str(value))
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        pass
    try:
        return "~/" + str(path.resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return path.name
