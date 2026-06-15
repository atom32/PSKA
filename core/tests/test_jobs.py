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
    service = JobService(store, worker_id="worker_a", lease_seconds=60)
    job = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})

    report = service.run_available(limit=1)
    completed = store.get_job(job.job_id)
    event_types = [event.event_type for event in store.list_job_events(job.job_id)]

    assert report.processed == 1
    assert report.succeeded == 1
    assert completed.status == "succeeded"
    assert completed.attempts == 1
    assert completed.worker_id == "worker_a"
    assert completed.heartbeat_at is not None
    assert completed.leased_until is None
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


def test_job_priority_controls_claim_order() -> None:
    store = _store()
    service = JobService(store)
    low = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"}, priority=1)
    high = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"}, priority=10)

    claimed = store.claim_next_job()

    assert claimed is not None
    assert claimed.job_id == high.job_id
    assert store.get_job(low.job_id).status == "queued"


def test_retryable_failure_uses_backoff_before_reclaim() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EMBED_BACKFILL, {"retry_backoff_seconds": 30}, max_attempts=2)

    report = service.run_available(limit=1)
    retried = store.get_job(job.job_id)

    assert report.processed == 1
    assert report.failed == 0
    assert retried.status == "queued"
    assert retried.run_after is not None
    assert retried.run_after > utc_now()
    assert store.claim_next_job() is None


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


def test_import_job_accepts_comma_separated_visible_team_ids(tmp_path) -> None:
    archive_dir = tmp_path / "zips"
    archive_dir.mkdir()
    _write_twitter_zip(archive_dir / "tweet.zip")
    store = _store()
    service = JobService(store)

    service.submit(
        IMPORT_TWITTER_ZIPS,
        {
            "input": str(archive_dir),
            "archive_root": str(tmp_path / "archive"),
            "visible_team_ids": "team_a, team_b",
        },
    )
    report = service.run_until_empty()
    item = next(iter(store.source_items.values()))

    assert report.succeeded == 1
    assert item.visible_team_ids == ["team_a", "team_b"]


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
    assert "command" not in completed.result
    assert str(tmp_path) not in json.dumps(completed.result)
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


def test_queued_and_running_jobs_can_be_canceled() -> None:
    store = _store()
    service = JobService(store)
    queued = service.submit(EMBED_BACKFILL, {}, max_attempts=1)
    running_source = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"}, max_attempts=1, priority=10)
    running = store.claim_next_job(worker_id="worker_cancel", lease_seconds=60)

    canceled_queued = store.cancel_job(queued.job_id, reason="not needed")
    canceled_running = store.cancel_job(running_source.job_id, reason="worker shutdown")

    assert running is not None
    assert running.job_id == running_source.job_id
    assert canceled_queued.status == "canceled"
    assert canceled_queued.error == "not needed"
    assert canceled_running.status == "canceled"
    assert canceled_running.worker_id is None
    assert canceled_running.leased_until is None
    assert [event.event_type for event in store.list_job_events(queued.job_id)][-1] == "canceled"


def test_stale_running_jobs_are_requeued_before_max_attempts() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"}, max_attempts=2)
    running = store.claim_next_job(worker_id="stale_worker", lease_seconds=60)
    assert running is not None
    running.started_at = utc_now() - timedelta(hours=2)

    recovered = service.recover_stale(max_age_seconds=60)

    assert [job.job_id for job in recovered] == [job.job_id]
    recovered_job = store.get_job(job.job_id)
    assert recovered_job.status == "queued"
    assert recovered_job.worker_id is None
    assert recovered_job.leased_until is None
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


def test_job_heartbeat_extends_lease_and_records_external_run() -> None:
    store = _store()
    service = JobService(store, worker_id="worker_heartbeat", lease_seconds=30)
    job = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})

    running = store.claim_next_job(worker_id="worker_heartbeat", lease_seconds=30)
    heartbeat = store.heartbeat_job(
        running.job_id,
        worker_id="worker_heartbeat",
        lease_seconds=90,
        external_run_id="run_abc",
    )

    assert heartbeat.status == "running"
    assert heartbeat.worker_id == "worker_heartbeat"
    assert heartbeat.external_run_id == "run_abc"
    assert heartbeat.heartbeat_at is not None
    assert heartbeat.leased_until is not None
    assert heartbeat.leased_until > heartbeat.heartbeat_at
    assert store.list_job_events(job.job_id)[-1].event_type == "heartbeat"


def test_lease_job_claims_specific_job_and_blocks_active_other_worker() -> None:
    store = _store()
    service = JobService(store)
    job = service.submit(EXTRACT_ALL, {"owner_user_id": "user_primary"})

    leased = store.lease_job(job.job_id, worker_id="worker_a", lease_seconds=90)

    assert leased.status == "running"
    assert leased.worker_id == "worker_a"
    assert leased.attempts == 1
    assert leased.leased_until is not None
    assert store.list_job_events(job.job_id)[-1].event_type == "leased"
    try:
        store.lease_job(job.job_id, worker_id="worker_b", lease_seconds=90)
    except ValueError as exc:
        assert "leased" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_job_source_refs_are_persisted_from_payload() -> None:
    store = _store()
    service = JobService(store)

    job = service.submit(
        EXTRACT_ALL,
        {
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": "src_1", "chunk_id": "chk_1"}],
        },
    )

    assert job.source_refs[0].source_item_id == "src_1"
    assert job.source_refs[0].chunk_id == "chk_1"
