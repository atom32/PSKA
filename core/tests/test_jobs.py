from __future__ import annotations

import json
from datetime import timedelta
import zipfile

from pska_core.enums import UserRole
from pska_core.ingest import IngestService
from pska_core.jobs import EMBED_BACKFILL, EXTRACT_ALL, FULL_REPORT, IMPORT_TWITTER_ZIPS, JobService
from pska_core.models import User, utc_now
from pska_core.store import InMemoryKnowledgeStore
from tests.fakes import FakeLLM, extraction_response


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    return store


class FakeEmbeddingProvider:
    provider_name = "fake-bge"
    model_name = "fake-model"
    dimensions = 3

    def __init__(self) -> None:
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0, 1.0] for _ in texts]


def _write_twitter_zip(zip_path) -> None:
    metadata = {
        "schema_version": "pska.archive.v2",
        "source": "twitter",
        "record_type": "tweet",
        "source_id": "123",
        "url": "https://x.com/u/status/123",
        "title": "Idempotent tweet",
        "content": {"text": "Project Atlas depends on the Twitter Archive channel."},
        "created_at": "2026-06-11T00:00:00Z",
        "captured_at": "2026-06-11T00:00:00Z",
        "author": {},
        "media": [],
    }
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("123/metadata.json", json.dumps(metadata))
        archive.writestr("123/content.md", "Project Atlas depends on the Twitter Archive channel.")


def test_job_service_runs_extract_all_job_and_records_events() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})

    report = service.run_available(limit=1)
    completed = store.get_job(job.job_id)
    event_types = [event.event_type for event in store.list_job_events(job.job_id)]

    assert report.processed == 1
    assert report.succeeded == 1
    assert completed.status == "succeeded"
    assert completed.attempts == 1
    assert completed.result == {"reports": []}
    assert event_types == ["queued", "started", "execute", "succeeded"]


def test_job_service_can_drain_queue_until_empty() -> None:
    store = _store()
    service = JobService(store)
    first = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})
    second = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})

    report = service.run_until_empty()

    assert report.processed == 2
    assert report.succeeded == 2
    assert store.get_job(first.job_id).status == "succeeded"
    assert store.get_job(second.job_id).status == "succeeded"


def test_import_job_retry_does_not_duplicate_source_document_or_chunk(tmp_path) -> None:
    archive_dir = tmp_path / "zips"
    archive_dir.mkdir()
    _write_twitter_zip(archive_dir / "tweet.zip")
    store = _store()
    service = JobService(store)
    payload = {"input": str(archive_dir), "archive_root": str(tmp_path / "archive")}
    service.submit(IMPORT_TWITTER_ZIPS, payload)
    service.submit(IMPORT_TWITTER_ZIPS, payload)

    report = service.run_until_empty()

    assert report.succeeded == 2
    assert len(store.source_items) == 1
    assert len(store.documents) == 1
    assert len(store.chunks) == 1


def test_embed_backfill_job_retry_skips_already_embedded_chunks() -> None:
    store = _store()
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "embed-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Embed note",
            "content": {"text": "A graph note."},
        }
    )
    provider = FakeEmbeddingProvider()
    service = JobService(store, embedding_provider=provider)
    service.submit(EMBED_BACKFILL, {"embedding_provider": "fake-bge"})
    service.submit(EMBED_BACKFILL, {"embedding_provider": "fake-bge"})

    report = service.run_until_empty()

    assert report.succeeded == 2
    assert provider.calls == 1
    chunk = next(iter(store.chunks.values()))
    assert chunk.embedding == [1.0, 0.0, 1.0]
    assert chunk.metadata["embedding_provider"] == "fake-bge"


def test_extract_job_retry_does_not_duplicate_entities_hyperedges_or_review_items() -> None:
    store = _store()
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "extract-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Extract note",
            "content": {
                "text": "Project Atlas depends on the Twitter Archive channel. "
                "The policy P-204 covers the education enrollment stage for dependent K. "
                "The Review Agent must confirm any team-visible sharing before release."
            },
        }
    )
    service = JobService(store, llm=FakeLLM([extraction_response(), extraction_response()]))
    service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})
    service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})

    report = service.run_until_empty()

    assert report.succeeded == 2
    assert len(store.entities) == 6
    assert len(store.hyperedges) == 3
    assert len(store.review_items) == 1


def test_job_service_runs_full_report_script_job(tmp_path) -> None:
    script = tmp_path / "report.py"
    output = tmp_path / "report.html"
    json_output = tmp_path / "report.json"
    script.write_text(
        "import json\n"
        "from pathlib import Path\n"
        f"Path({str(output)!r}).write_text('<html>ok</html>')\n"
        f"Path({str(json_output)!r}).write_text(json.dumps({{'ok': True}}))\n",
        encoding="utf-8",
    )
    store = _store()
    service = JobService(store)
    job = service.submit(
        FULL_REPORT,
        {
            "script_path": str(script),
            "args": [],
            "output": str(output),
            "json_output": str(json_output),
            "timeout": 10,
        },
    )

    report = service.run_available(limit=1)
    completed = store.get_job(job.job_id)

    assert report.succeeded == 1
    assert completed.status == "succeeded"
    assert completed.result["returncode"] == 0
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == {"ok": True}


def test_job_service_marks_non_retryable_failure_after_max_attempts() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EMBED_BACKFILL, {}, max_attempts=1)

    report = service.run_available(limit=1)
    failed = store.get_job(job.job_id)

    assert report.failed == 1
    assert failed.status == "failed"
    assert failed.attempts == 1
    assert "ValueError" in (failed.error or "")


def test_failed_jobs_can_be_requeued_for_manual_retry() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EMBED_BACKFILL, {}, max_attempts=1)
    service.run_available(limit=1)

    retried = store.retry_job(job.job_id)

    assert retried.status == "queued"
    assert retried.error is None
    assert store.list_job_events(job.job_id)[-1].event_type == "retry_queued"


def test_stale_running_jobs_are_requeued_before_max_attempts() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"}, max_attempts=2)
    running = store.claim_next_job()
    assert running is not None
    running.started_at = utc_now() - timedelta(hours=2)

    recovered = service.recover_stale(max_age_seconds=60)

    assert [job.job_id for job in recovered] == [job.job_id]
    assert store.get_job(job.job_id).status == "queued"
    assert store.list_job_events(job.job_id)[-1].event_type == "stale_requeued"


def test_stale_running_jobs_fail_after_max_attempts() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"}, max_attempts=1)
    running = store.claim_next_job()
    assert running is not None
    running.started_at = utc_now() - timedelta(hours=2)

    recovered = service.recover_stale(max_age_seconds=60)

    assert [job.job_id for job in recovered] == [job.job_id]
    assert store.get_job(job.job_id).status == "failed"
    assert store.list_job_events(job.job_id)[-1].event_type == "stale_failed"
