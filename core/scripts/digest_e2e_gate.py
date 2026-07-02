from __future__ import annotations

import argparse
from uuid import uuid4
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pska_core.api import PSKAApi
from pska_core.candidates import CandidateWriteError, CandidateWriteService
from pska_core.enums import UserRole, Visibility
from pska_core.fastreact_client import FastreactConfig, FastreactError, HttpFastreactClient
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.models import User
from pska_core.serde import dumps, to_jsonable
from pska_core.store_postgres import PostgresKnowledgeStore


DEFAULT_DATABASE_URL = "postgresql:///pska"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = PostgresKnowledgeStore(args.database_url)
    report = build_digest_e2e_gate_report(args, store=store)
    print(dumps(report))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a repeatable PSKA digest E2E write-back gate.")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--owner-user-id", default="user_primary")
    parser.add_argument("--worker-id", default="digest-e2e-gate-worker")
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--fastreact-url", default="http://127.0.0.1:18741")
    parser.add_argument("--fastreact-timeout-seconds", type=float, default=2.0)
    parser.add_argument(
        "--require-fastreact-online",
        action="store_true",
        help="Fail the gate when FastReAct health/readiness is unavailable or missing PSKA tools.",
    )
    return parser


def build_digest_e2e_gate_report(args: argparse.Namespace, *, store: Any) -> dict[str, Any]:
    _ensure_gate_users(store, args.owner_user_id)
    api = _api_for_store(store)
    source = _ingest_gate_source(store, owner_user_id=args.owner_user_id)
    scheduled = api.schedule_digest(
        {
            "owner_user_id": args.owner_user_id,
            "source_item_ids": [source.source_item_id],
            "limit": 1,
            "batch_size": args.batch_size,
            "priority": 50,
            "max_attempts": 1,
            "force": True,
            "reason": "HW-009 digest E2E write-back gate",
        }
    )
    job = scheduled.get("job")
    contract_checks: dict[str, bool] = {
        "scheduled": bool(job),
        "scheduled_source_refs": scheduled.get("scheduled_source_item_ids") == [source.source_item_id],
    }
    steps: dict[str, Any] = {
        "source": {
            "source_item_id": source.source_item_id,
            "title": source.title,
        },
        "schedule": scheduled,
    }
    diagnostics: list[str] = []

    if not job:
        diagnostics.append("digest schedule did not create a job")
        return _report(args, contract_checks, steps, diagnostics, fastreact=_fastreact_report(args))

    job_id = str(job["job_id"])
    lease = api.lease_job(job_id, {"worker_id": args.worker_id, "lease_seconds": args.lease_seconds})
    context = api.job_context(job_id, limit=args.batch_size)
    source_refs = [{"source_item_id": source.source_item_id}]
    missing_source_refs = _missing_source_refs_write(api, job_id=job_id, owner_user_id=args.owner_user_id)
    candidate_write = api.write_candidates(
        {
            "schema_version": "pska.candidates.v1",
            "owner_user_id": args.owner_user_id,
            "job_id": job_id,
            "request_id": f"hw009-{uuid4().hex}",
            "producer": "digest_e2e_gate",
            "source_refs": source_refs,
            "entities": [
                {
                    "entity_type": "workflow",
                    "label": "HW-009 Digest E2E Gate",
                    "confidence": 0.9,
                }
            ],
            "review_items": [
                {
                    "review_type": "action_candidate",
                    "title": "Review HW-009 digest gate canary",
                    "source_refs": source_refs,
                    "proposal": {
                        "action": "inspect_digest_gate_output",
                        "source_refs": source_refs,
                        "confidence": 0.82,
                    },
                }
            ],
            "memory_candidates": [
                {
                    "kind": "agent_memory",
                    "layer": "semantic",
                    "text": "HW-009 digest gate verified PSKA write-back with grounded source refs.",
                    "confidence": 0.8,
                    "source_refs": source_refs,
                }
            ],
        }
    )
    complete = api.complete_job(
        job_id,
        {
            "result": {
                "ok": True,
                "gate": "HW-009 digest E2E write-back",
                "candidate_write": candidate_write["summary"],
            }
        },
    )
    fail_path = _run_fail_path(api, source_item_id=source.source_item_id, owner_user_id=args.owner_user_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds)
    final = store.get_job(job_id)
    events = store.list_job_events(job_id)

    steps.update(
        {
            "lease": lease,
            "context": _context_summary(context),
            "missing_source_refs_write": missing_source_refs,
            "candidate_write": candidate_write,
            "complete": complete,
            "fail_path": fail_path,
            "events": [event.event_type for event in events],
        }
    )
    summary = candidate_write["summary"]
    contract_checks.update(
        {
            "leased": lease["job"]["status"] == "running",
            "context_has_source": source.source_item_id in [item["source_item_id"] for item in context.get("source_items", [])],
            "missing_source_refs_rejected": missing_source_refs["rejected"] is True,
            "candidate_write_has_source_refs": _summary_outputs_have_source_refs(store, summary),
            "candidate_or_review_or_memory_written": any(
                summary.get(key)
                for key in ["review_items", "agent_memories", "profile_cards", "hyperedges", "entities"]
            ),
            "completed": final.status == "succeeded",
            "complete_result_recorded": bool(final.result.get("candidate_write")),
            "fail_path_failed": fail_path["job"]["status"] == "failed",
            "fail_path_error_recorded": "simulated FastReAct failure" in (fail_path["job"].get("error") or ""),
        }
    )
    fastreact = _fastreact_report(args)
    return _report(args, contract_checks, steps, diagnostics, fastreact=fastreact)


def _report(
    args: argparse.Namespace,
    contract_checks: dict[str, bool],
    steps: dict[str, Any],
    diagnostics: list[str],
    *,
    fastreact: dict[str, Any],
) -> dict[str, Any]:
    if args.require_fastreact_online and not fastreact.get("ok"):
        diagnostics.append("FastReAct is required but not available with the PSKA worker tool contract")
    contract_ok = all(contract_checks.values())
    fastreact_required_ok = (not args.require_fastreact_online) or bool(fastreact.get("ok"))
    return {
        "ok": contract_ok and fastreact_required_ok,
        "database_url": getattr(args, "database_url", DEFAULT_DATABASE_URL),
        "owner_user_id": args.owner_user_id,
        "contract": {
            "ok": contract_ok,
            "checks": contract_checks,
        },
        "fastreact": fastreact,
        "steps": to_jsonable(steps),
        "diagnostics": diagnostics,
    }


def _api_for_store(store: Any) -> PSKAApi:
    api = object.__new__(PSKAApi)
    api.store = store
    api.jobs = JobService(store)
    api.candidates = CandidateWriteService(store)
    return api


def _ensure_gate_users(store: Any, owner_user_id: str) -> None:
    store.add_user(User(owner_user_id, owner_user_id, UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))


def _ingest_gate_source(store: Any, *, owner_user_id: str):
    run_id = uuid4().hex
    return IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual_canary",
            "record_type": "digest_e2e_gate",
            "source_id": f"hw009-digest-e2e-gate-{run_id}",
            "owner_user_id": owner_user_id,
            "space_id": "private_primary",
            "visibility": Visibility.PRIVATE.value,
            "title": "HW-009 digest E2E gate canary",
            "content": {
                "text": (
                    "HW-009 digest E2E gate canary. "
                    "This source verifies schedule, lease, context, grounded candidate write-back, complete, and fail diagnostics."
                )
            },
            "extra": {"gate": "HW-009", "run_id": run_id},
        }
    )


def _missing_source_refs_write(api: PSKAApi, *, job_id: str, owner_user_id: str) -> dict[str, Any]:
    try:
        api.write_candidates(
            {
                "schema_version": "pska.candidates.v1",
                "owner_user_id": owner_user_id,
                "job_id": job_id,
                "producer": "digest_e2e_gate",
                "entities": [{"entity_type": "workflow", "label": "Ungrounded digest write"}],
            }
        )
    except CandidateWriteError as exc:
        return {"rejected": True, "error": str(exc)}
    return {"rejected": False, "error": ""}


def _run_fail_path(api: PSKAApi, *, source_item_id: str, owner_user_id: str, worker_id: str, lease_seconds: int) -> dict[str, Any]:
    failed_job = JobService(api.store).submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": owner_user_id,
            "source_refs": [{"source_item_id": source_item_id}],
            "scope": {"source_item_ids": [source_item_id]},
        },
        max_attempts=1,
    )
    api.lease_job(failed_job.job_id, {"worker_id": worker_id, "lease_seconds": lease_seconds})
    return api.fail_job(failed_job.job_id, {"error": "simulated FastReAct failure for HW-009 gate", "retryable": False})


def _context_summary(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": context["job"]["job_id"],
        "source_item_ids": [item["source_item_id"] for item in context.get("source_items", [])],
        "chunk_ids": [chunk["chunk_id"] for chunk in context.get("chunks", [])],
        "has_more": context.get("has_more"),
        "next_cursor": context.get("next_cursor"),
        "total_source_items": context.get("total_source_items"),
    }


def _summary_outputs_have_source_refs(store: Any, summary: dict[str, Any]) -> bool:
    checks: list[bool] = []
    owner_user_id = _summary_owner_user_id(store, summary)
    for review_item_id in summary.get("review_items") or []:
        review = store.get_review_item(review_item_id)
        checks.append(bool(review.proposal.get("source_refs")))
    for agent_memory_id in summary.get("agent_memories") or []:
        memory = store.get_agent_memory(agent_memory_id)
        checks.append(bool(memory.source_refs))
    for profile_card_id in summary.get("profile_cards") or []:
        profile_card = next(card for card in store.list_profile_cards(owner_user_id=owner_user_id) if card.profile_card_id == profile_card_id)
        checks.append(bool(profile_card.source_refs))
    for entity_id in summary.get("entities") or []:
        entity = next(entity for entity in store.list_entities() if entity.entity_id == entity_id)
        checks.append(bool((entity.metadata or {}).get("source_refs")))
    for hyperedge_id in summary.get("hyperedges") or []:
        for hyperedge, _members in store.list_hyperedges_for_entities(set()):
            if hyperedge.hyperedge_id == hyperedge_id:
                checks.append(bool(hyperedge.source_refs))
                break
    return bool(checks) and all(checks)


def _summary_owner_user_id(store: Any, summary: dict[str, Any]) -> str:
    for review_item_id in summary.get("review_items") or []:
        return store.get_review_item(review_item_id).owner_user_id
    for agent_memory_id in summary.get("agent_memories") or []:
        return store.get_agent_memory(agent_memory_id).owner_user_id
    return "user_primary"


def _fastreact_report(args: argparse.Namespace) -> dict[str, Any]:
    client = HttpFastreactClient(
        FastreactConfig(
            url=str(args.fastreact_url).rstrip("/"),
            timeout_seconds=args.fastreact_timeout_seconds,
        )
    )
    try:
        ready = client.ready()
    except FastreactError as exc:
        return {
            "ok": False,
            "url": str(args.fastreact_url).rstrip("/"),
            "required": bool(args.require_fastreact_online),
            "error": str(exc),
            "diagnostic": "FastReAct is unavailable; PSKA contract smoke passed only if contract.ok=true.",
            "worker_command_hint": "./scripts/pska fastreact-digest-worker-command",
        }
    ok = bool(ready.get("ok")) and bool(ready.get("pska_tools_loaded"))
    return {
        "ok": ok,
        "url": ready.get("url") or str(args.fastreact_url).rstrip("/"),
        "required": bool(args.require_fastreact_online),
        "pska_tools_loaded": ready.get("pska_tools_loaded"),
        "missing_pska_tools": ready.get("missing_pska_tools") or [],
        "ready": ready,
        "diagnostic": "FastReAct is online with required PSKA tools" if ok else "FastReAct is online but missing required PSKA tools",
    }


if __name__ == "__main__":
    raise SystemExit(main())
