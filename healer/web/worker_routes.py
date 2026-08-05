"""Authenticated Cloud Tasks handlers for on-demand enumeration."""
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from healer.web import job_store
from healer.web.interface import format_enumeration_results, run_molecule_enumeration, run_site_enumeration

router = APIRouter(prefix="/internal/tasks")


class EnumerationTask(BaseModel):
    job_id: str
    params: dict[str, Any]


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
