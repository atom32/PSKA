from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from pska_core.embeddings import EmbeddingConfig, EmbeddingProvider, EmbeddingService, build_embedding_provider
from pska_core.enums import Visibility
from pska_core.extraction import ExtractionService
from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.models import Job
from pska_core.serde import to_jsonable
from pska_core.llm import LLMClient
from pska_core.store_postgres import PostgresKnowledgeStore

IMPORT_TWITTER_ZIPS = "import_twitter_zips"
EXTRACT_ALL = "extract_all"
EMBED_BACKFILL = "embed_backfill"
FULL_REPORT = "full_report"

JOB_TYPES = {IMPORT_TWITTER_ZIPS, EXTRACT_ALL, EMBED_BACKFILL, FULL_REPORT}


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
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider
        self.llm = llm

    def submit(self, job_type: str, payload: dict[str, Any] | None = None, *, max_attempts: int = 3) -> Job:
        if job_type not in JOB_TYPES:
            raise ValueError(f"Unsupported job type: {job_type}")
        return self.store.create_job(job_type, payload or {}, max_attempts=max_attempts)

    def run_next(self) -> Job | None:
        job = self.store.claim_next_job()
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
        if job.job_type == EMBED_BACKFILL:
            return self._embed_backfill(job.payload)
        if job.job_type == FULL_REPORT:
            return self._full_report(job)
        raise ValueError(f"Unsupported job type: {job.job_type}")

    def _import_twitter_zips(self, payload: dict[str, Any]) -> dict[str, Any]:
        embedding_provider = self._embedding_provider(payload)
        importer = TwitterZipImporter(
            self.store,
            archive_root=Path(payload.get("archive_root") or "archive/imports"),
            owner_user_id=str(payload.get("owner_user_id") or "user_primary"),
            space_id=str(payload.get("space_id") or "private_primary"),
            visibility=Visibility(payload.get("visibility") or Visibility.PRIVATE.value),
            visible_team_ids=_visible_team_ids(payload.get("visible_team_ids")),
            embedding_provider=embedding_provider,
        )
        result = importer.import_directory(Path(payload.get("input") or Path.home() / "Downloads" / "twitter_archive"))
        return to_jsonable(result)

    def _extract_all(self, payload: dict[str, Any]) -> dict[str, Any]:
        reports = ExtractionService(self.store, llm=self.llm).extract_all_visible(owner_user_id=payload.get("owner_user_id"))
        return {"reports": to_jsonable(reports)}

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
            output_path = str(payload.get("output") or core_root / "reports" / f"job_{job.job_id}.html")
            json_output_path = str(payload.get("json_output") or core_root / "reports" / f"job_{job.job_id}.json")
            command = [
                sys.executable,
                str(script_path),
                "--input",
                str(payload.get("input") or Path.home() / "Downloads" / "twitter_archive"),
                "--database-url",
                str(payload.get("database_url") or getattr(self.store, "database_url", "postgresql:///pska_smoke")),
                "--archive-root",
                str(payload.get("archive_root") or core_root / "archive" / "imports"),
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
