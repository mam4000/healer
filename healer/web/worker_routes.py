"""Authenticated Cloud Tasks handlers for on-demand enumeration."""
import logging
import resource
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from healer.web import job_store
from healer.web.cloud_tasks import TaskSubmissionError, create_molport_merge_task
from healer.web.interface import (
    format_enumeration_results,
    run_molecule_enumeration,
    run_site_enumeration,
    run_molport_merge,
    run_molport_shard_selection,
)

router = APIRouter(prefix="/internal/tasks")
logger = logging.getLogger(__name__)


def _peak_rss_mib() -> float:
    """Return this worker process's high-water memory mark on Linux Cloud Run."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class EnumerationTask(BaseModel):
    job_id: str
    params: dict[str, Any]


class MolportShardTask(EnumerationTask):
    shard_id: str
    shard_name: str


def _run(job_id: str, job_type: str, params: dict[str, Any]) -> None:
    job = job_store.get(job_id)
    if job is None or job["status"] == "CANCELLED":
        return
    job_store.update(job_id, status="STARTED")
    try:
        raw_results = run_molecule_enumeration(**params) if job_type == "molecule" else run_site_enumeration(**params)
        display, complete = format_enumeration_results(raw_results, job_type)
        job_store.update(job_id, status="SUCCESS", result={"display": display, "complete": complete})
    except Exception:
        job_store.update(job_id, status="FAILURE")
        raise


@router.post("/enumerate/{job_type}")
async def enumerate_task(job_type: str, request: EnumerationTask):
    if job_type not in {"molecule", "site"}:
        raise HTTPException(status_code=404, detail="Unknown enumeration type")
    try:
        _run(request.job_id, job_type, request.params)
    except Exception as exc:
        # A 5xx response makes Cloud Tasks retry according to its queue policy.
        raise HTTPException(status_code=500, detail="Enumeration failed") from exc
    return {"job_id": request.job_id, "status": "complete"}


@router.post("/molport-shard")
async def molport_shard_task(request: MolportShardTask):
    job = job_store.get(request.job_id)
    if job is None or job["status"] in {"CANCELLED", "FAILURE", "SUCCESS"}:
        return {"job_id": request.job_id, "status": "ignored"}
    try:
        candidates = run_molport_shard_selection(request.params, request.shard_name)
    except Exception as exc:
        logger.exception(
            "Molport shard selection failed: job=%s shard=%s shard_name=%s",
            request.job_id,
            request.shard_id,
            request.shard_name,
        )
        job_store.record_shard_failure(request.job_id, request.shard_id)
        raise HTTPException(status_code=500, detail="Molport shard selection failed") from exc
    queue_merge = job_store.record_shard_success(request.job_id, request.shard_id, candidates)
    logger.warning(
        "Molport shard worker memory peak: job=%s shard=%s peak_rss_mib=%.1f",
        request.job_id,
        request.shard_id,
        _peak_rss_mib(),
    )
    if queue_merge:
        try:
            create_molport_merge_task(request.job_id, request.params)
        except TaskSubmissionError as exc:
            # A Cloud Tasks retry of this shard will reclaim and enqueue it.
            job_store.update(request.job_id, merge_claimed=False, phase="SCANNING")
            raise HTTPException(status_code=500, detail="Unable to queue Molport merge") from exc
    return {"job_id": request.job_id, "shard_id": request.shard_id, "status": "complete"}


@router.post("/molport-merge")
async def molport_merge_task(request: EnumerationTask):
    job = job_store.get(request.job_id)
    if job is None or job["status"] == "CANCELLED":
        return {"job_id": request.job_id, "status": "ignored"}
    if job["status"] == "FAILURE":
        return {"job_id": request.job_id, "status": "ignored"}
    try:
        if len(job.get("completed_shards", [])) != job.get("total_shards", 0):
            raise ValueError("Molport merge arrived before all shard results")
        job_store.update(request.job_id, status="STARTED", phase="MERGING")
        shard_results = [job["shard_results"][shard_id] for shard_id in sorted(job["shard_results"])]
        raw_results = run_molport_merge(request.params, shard_results)
        display, complete = format_enumeration_results(raw_results, "molecule")
        job_store.update(
            request.job_id, status="SUCCESS", phase="COMPLETE", result={"display": display, "complete": complete}
        )
        logger.warning("Molport merge worker memory peak: job=%s peak_rss_mib=%.1f", request.job_id, _peak_rss_mib())
    except Exception as exc:
        job_store.update(request.job_id, status="FAILURE", phase="FAILED")
        raise HTTPException(status_code=500, detail="Molport result merge failed") from exc
    return {"job_id": request.job_id, "status": "complete"}
