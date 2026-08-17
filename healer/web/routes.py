'''
    FastAPI routes for HEALER web application.
    
    Supports two modes:
    - Local mode (default): Jobs run synchronously, no Redis/Celery needed
    - Server mode: Jobs run through Cloud Tasks to on-demand Cloud Run workers
    
    Set HEALER_SERVER_MODE=true to enable server mode.
'''
import os
import uuid
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Dict, Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from rdkit import Chem
from rdkit.Chem import rdDepictor, Descriptors, QED

from healer.web.models import (
    MoleculeRequest,
    SiteRequest,
    BatchMoleculeRequest,
    BatchSiteRequest,
    JobSubmitResponse,
    BatchJobSubmitResponse,
    BatchStatusRequest,
    BatchJobProgress,
    BatchStatusResponse,
    JobStatusResponse,
    JobResult,
)
from healer.web.interface import (
    SERVER_MODE,
    run_molecule_enumeration,
    run_site_enumeration,
    format_enumeration_results,
    get_server_limits,
    discover_building_blocks,
    apply_server_limits,
)
from healer.web import job_store
from healer.web.auth import current_principal, require_principal
from healer.web.cloud_tasks import (
    TaskSubmissionError,
    create_enumeration_task,
    create_molport_shard_task,
    delete_task,
    molport_merge_task_name_for,
    molport_shard_task_name_for,
    task_name_for,
)
from healer.domain.bb_repository import MOLPORT_FULL_SOURCE, ShardedBBRepository, get_repository
from healer.utils import utils

router = APIRouter(prefix="/api", dependencies=[Depends(require_principal)])

# ============================================================================
# Mode Detection
# ============================================================================

USE_CELERY = SERVER_MODE

if USE_CELERY:
    print("HEALER Web: Running in SERVER mode (Cloud Tasks)")
else:
    print("HEALER Web: Running in LOCAL mode (synchronous)")

# ============================================================================
# In-Memory Job Store (for local mode)
# ============================================================================

_local_jobs: Dict[str, Dict[str, Any]] = {}
logger = logging.getLogger(__name__)

# Batch submissions run one job per molecule in the background (local mode only;
# server mode already dispatches each job asynchronously via Cloud Tasks).
_local_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("HEALER_LOCAL_WORKERS", "4")))

MAX_BATCH_SIZE = int(os.environ.get("HEALER_MAX_BATCH_SIZE", "50"))


def _submit_molport_fanout(job_id: str, params: dict[str, Any], owner: str) -> None:
    repo = get_repository(MOLPORT_FULL_SOURCE)
    if not isinstance(repo, ShardedBBRepository):
        raise RuntimeError("Molport repository is not configured as a sharded source")
    shard_names = [path.name for path in repo.shard_paths()]
    if not shard_names:
        raise ValueError("No processed Molport shards are available")
    shard_tasks = {
        str(index): molport_shard_task_name_for(job_id, str(index))
        for index in range(len(shard_names))
    }
    logger.warning("Molport fan-out submitted: job=%s shards=%d", job_id, len(shard_names))
    job_store.create_fanout(job_id, shard_tasks, molport_merge_task_name_for(job_id), params, owner)
    try:
        for index, shard_name in enumerate(shard_names):
            create_molport_shard_task(job_id, str(index), shard_name, params)
    except Exception:
        for task_name in shard_tasks.values():
            try:
                delete_task(task_name)
            except TaskSubmissionError:
                pass
        job_store.update(job_id, status="FAILURE", phase="FAILED")
        raise


def _run_job_sync(job_id: str, job_type: str, params: dict, owner: str = "standalone:local") -> None:
    """Run a job synchronously and store results."""
    _local_jobs[job_id] = {"status": "STARTED", "result": None, "error": None, "owner": owner}

    try:
        if job_type == "molecule":
            raw_results = run_molecule_enumeration(**params)
            display_res, complete_res = format_enumeration_results(raw_results, 'molecule')
        else:  # site
            raw_results = run_site_enumeration(**params)
            display_res, complete_res = format_enumeration_results(raw_results, 'site')

        _local_jobs[job_id] = {
            "status": "SUCCESS",
            "result": {"display": display_res, "complete": complete_res},
            "error": None,
            "owner": owner,
        }
    except Exception as e:
        _local_jobs[job_id] = {
            "status": "FAILURE",
            "result": None,
            "error": str(e),
            "owner": owner,
        }


def _run_job_async(job_id: str, job_type: str, params: dict, owner: str = "standalone:local") -> None:
    """Queue a local-mode job to run in the background so a batch submission
    doesn't block on every molecule finishing before returning job ids."""
    _local_jobs[job_id] = {"status": "PENDING", "result": None, "error": None, "owner": owner}
    _local_executor.submit(_run_job_sync, job_id, job_type, params, owner)


def _create_server_job(job_type: str, params: dict[str, Any], owner: str) -> str:
    """Submit one job's worth of params as a Cloud Tasks job and return its id."""
    job_id = str(uuid.uuid4())
    try:
        params = apply_server_limits(params, job_type)
        if job_type == "molecule" and params.get("bb_source") == MOLPORT_FULL_SOURCE:
            _submit_molport_fanout(job_id, params, owner)
        else:
            task_name = task_name_for(job_id)
            job_store.create(job_id, task_name, owner)
            create_enumeration_task(job_id, job_type, params)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (TaskSubmissionError, job_store.JobStoreUnavailableError) as exc:
        print(f"Unable to submit {job_type} job: {exc!r}; cause={exc.__cause__!r}")
        raise HTTPException(status_code=503, detail="The job queue is temporarily unavailable") from exc
    return job_id


def _require_job_owner(job: dict[str, Any]) -> None:
    """Hide jobs from other principals, including callers who guessed a UUID."""
    if job.get("owner") != current_principal().owner_key:
        raise HTTPException(status_code=404, detail="Job not found")


# ============================================================================
# Enumeration Endpoints
# ============================================================================

@router.post("/enumerate/molecule", response_model=JobSubmitResponse)
async def submit_molecule_enumeration(request: MoleculeRequest):
    params = request.model_dump() if hasattr(request, "model_dump") else request.dict()

    if USE_CELERY:
        job_id = _create_server_job("molecule", params, current_principal().owner_key)
        return JobSubmitResponse(job_id=job_id, status="submitted")
    else:
        # Local synchronous mode
        job_id = str(uuid.uuid4())
        _run_job_sync(job_id, "molecule", params, current_principal().owner_key)
        return JobSubmitResponse(job_id=job_id, status="submitted")


@router.post("/enumerate/site", response_model=JobSubmitResponse)
async def submit_site_enumeration(request: SiteRequest):
    params = request.model_dump() if hasattr(request, "model_dump") else request.dict()

    if USE_CELERY:
        job_id = _create_server_job("site", params, current_principal().owner_key)
        return JobSubmitResponse(job_id=job_id, status="submitted")
    else:
        # Local synchronous mode
        job_id = str(uuid.uuid4())
        _run_job_sync(job_id, "site", params, current_principal().owner_key)
        return JobSubmitResponse(job_id=job_id, status="submitted")


@router.post("/enumerate/molecule/batch", response_model=BatchJobSubmitResponse)
async def submit_molecule_batch(request: BatchMoleculeRequest):
    """Submit one enumeration job per SMILES. Jobs run independently — poll
    GET /jobs/{job_id} for each id to get results as they finish, rather than
    waiting for the whole batch to complete."""
    if len(request.molecules) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=422, detail=f"Batch size exceeds the limit of {MAX_BATCH_SIZE} molecules")

    base_params = request.model_dump(exclude={"molecules"})
    job_ids = []
    for smiles in request.molecules:
        params = {**base_params, "molecule": smiles}
        if USE_CELERY:
            job_ids.append(_create_server_job("molecule", params, current_principal().owner_key))
        else:
            job_id = str(uuid.uuid4())
            _run_job_async(job_id, "molecule", params, current_principal().owner_key)
            job_ids.append(job_id)
    return BatchJobSubmitResponse(job_ids=job_ids, status="submitted")


@router.post("/enumerate/site/batch", response_model=BatchJobSubmitResponse)
async def submit_site_batch(request: BatchSiteRequest):
    """Submit one site-enumeration job per SMILES; see submit_molecule_batch."""
    if len(request.molecules) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=422, detail=f"Batch size exceeds the limit of {MAX_BATCH_SIZE} molecules")

    base_params = request.model_dump(exclude={"molecules"})
    job_ids = []
    for smiles in request.molecules:
        params = {**base_params, "molecule": smiles}
        if USE_CELERY:
            job_ids.append(_create_server_job("site", params, current_principal().owner_key))
        else:
            job_id = str(uuid.uuid4())
            _run_job_async(job_id, "site", params, current_principal().owner_key)
            job_ids.append(job_id)
    return BatchJobSubmitResponse(job_ids=job_ids, status="submitted")


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    if USE_CELERY:
        try:
            job = job_store.get(job_id)
        except job_store.JobStoreUnavailableError as exc:
            raise HTTPException(status_code=503, detail="The job store is temporarily unavailable") from exc
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        _require_job_owner(job)
        response = JobStatusResponse(
            job_id=job_id,
            status=job["status"],
            completed_shards=len(job.get("completed_shards", [])) if "total_shards" in job else None,
            total_shards=job.get("total_shards"),
            phase=job.get("phase"),
        )
        if job["status"] == "SUCCESS":
            response.result = JobResult(**job["result"])
        elif job["status"] == "FAILURE":
            response.error = "The job failed. Please try again later."
        return response
    else:
        # Local mode
        if job_id not in _local_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = _local_jobs[job_id]
        _require_job_owner(job)
        response = JobStatusResponse(job_id=job_id, status=job["status"])
        
        if job["status"] == "SUCCESS":
            response.result = JobResult(**job["result"])
        elif job["status"] == "FAILURE":
            response.error = job["error"]
        
        return response


@router.post("/jobs/batch-status", response_model=BatchStatusResponse)
async def get_batch_status(request: BatchStatusRequest):
    """Return current status for a set of job ids, for driving a batch progress indicator."""
    jobs = []
    completed = 0
    failed = 0
    for job_id in request.job_ids:
        if USE_CELERY:
            try:
                job = job_store.get(job_id)
            except job_store.JobStoreUnavailableError as exc:
                raise HTTPException(status_code=503, detail="The job store is temporarily unavailable") from exc
        else:
            job = _local_jobs.get(job_id)

        if job and job.get("owner") != current_principal().owner_key:
            job = None
        status = job["status"] if job else "UNKNOWN"
        if status == "SUCCESS":
            completed += 1
        elif status in ("FAILURE", "CANCELLED"):
            failed += 1
        jobs.append(BatchJobProgress(job_id=job_id, status=status))

    return BatchStatusResponse(total=len(request.job_ids), completed=completed, failed=failed, jobs=jobs)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel a running or pending job."""
    if USE_CELERY:
        try:
            job = job_store.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Job not found")
            _require_job_owner(job)
            task_names = list(job.get("task_names", {}).values())
            if "task_name" in job:
                task_names.append(job["task_name"])
            if job.get("merge_task_name"):
                task_names.append(job["merge_task_name"])
            for task_name in task_names:
                delete_task(task_name)
            job_store.update(job_id, status="CANCELLED")
            return {"job_id": job_id, "status": "cancelled"}
        except TaskSubmissionError as e:
            print(f"Failed to cancel job {job_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to cancel job")
    else:
        # Local mode - can't really cancel synchronous jobs
        # But we can mark it as cancelled if it exists
        if job_id in _local_jobs:
            _require_job_owner(_local_jobs[job_id])
            _local_jobs[job_id]["status"] = "CANCELLED"
            return {"job_id": job_id, "status": "cancelled", "note": "Local mode - job may have already completed"}
        raise HTTPException(status_code=400, detail="Cannot cancel jobs in local mode")


@router.get("/info/mode")
async def get_server_mode():
    """Return the current server mode (for UI to know if cancel is available)."""
    return {"mode": "tasks" if USE_CELERY else "local"}


@router.get("/info/limits")
async def get_server_limits_endpoint():
    """Return the server parameter limits for UI validation."""
    return {
        "server_mode": SERVER_MODE,
        "limits": get_server_limits()
    }


@router.get("/info/building-blocks")
async def get_available_building_blocks():
    """Return list of available building block libraries."""
    return {"building_blocks": discover_building_blocks()}


@router.get("/jobs/{job_id}/download")
async def download_job_results(job_id: str):
    if USE_CELERY:
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        _require_job_owner(job)
        if job["status"] != 'SUCCESS':
            raise HTTPException(status_code=400, detail="Job not completed or failed")
        results = job["result"].get('complete', [])
    else:
        if job_id not in _local_jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        job = _local_jobs[job_id]
        _require_job_owner(job)
        if job["status"] != "SUCCESS":
            raise HTTPException(status_code=400, detail="Job not completed or failed")
        results = job["result"].get('complete', [])
    
    if not results:
        raise HTTPException(status_code=404, detail="No results found")
    
    df = pd.DataFrame(results)
    stream = io.StringIO()
    df.to_csv(stream, index=False)
    
    response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=healer_results_{job_id}.csv"
    return response


# ============================================================================
# Utility Endpoints
# ============================================================================

class SmilesRequest(BaseModel):
    smiles: str

class RenderRequest(BaseModel):
    smiles: str
    bbs: Optional[List[str]] = None
    alpha: float = 0.4
    bgColor: str = 'rgba(255, 255, 255, 1.0)'


@router.get("/utils/reaction-tags")
async def get_reaction_tags():
    """Return a list of available reaction tags."""
    try:
        # Reaction tags always come from package data
        healer_pkg = Path(__file__).parent.parent
        reaction_tags_path = healer_pkg / 'data' / 'reactions' / 'reaction_tags.txt'
        
        if not reaction_tags_path.exists():
            print("Warning: reaction_tags.txt not found, returning default tags")
            return ["amide coupling", "amide", "C-N bond formation", "C-N",
                    "alkylation", "N-arylation", "azole", "amination"]

        with open(reaction_tags_path, 'r') as f:
            tags = [line.strip() for line in f if line.strip()]
        
        # Exclude "all" in server mode
        if SERVER_MODE:
            tags = [tag for tag in tags if tag.lower() != 'all']
            
        return tags
            
    except Exception as e:
        print(f"Error loading reaction tags: {e}")
        return []


@router.post("/utils/smiles-to-mol")
async def smiles_to_molfile(request: SmilesRequest):
    try:
        smiles = request.smiles.strip()
        if not smiles:
            raise HTTPException(status_code=400, detail="No SMILES provided")
        
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            raise HTTPException(status_code=400, detail="Invalid SMILES format")

        rdDepictor.Compute2DCoords(mol)
        molblock = Chem.MolToMolBlock(mol)
        
        return {"molblock": molblock}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error converting SMILES: {e}")
        raise HTTPException(status_code=500, detail="Error processing molecule")


@router.post("/utils/render-mol-with-indices")
async def render_mol_with_indices(request: SmilesRequest):
    """Return a base64 SVG of the molecule with atom indices labeled, plus properties."""
    try:
        smiles = request.smiles.strip()
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            raise HTTPException(status_code=400, detail="Invalid SMILES")

        props = {
            "MW": round(Descriptors.MolWt(mol), 2),
            "LogP": round(Descriptors.MolLogP(mol), 2),
            "HBA": Descriptors.NumHAcceptors(mol),
            "HBD": Descriptors.NumHDonors(mol),
            "TPSA": round(Descriptors.TPSA(mol), 2),
            "QED": round(QED.qed(mol), 3)
        }

        svg_data_uri = utils.get_svg_mol(mol, legend="", show_idx=True, width=250, height=125)
        return {"svg": svg_data_uri, "properties": props}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error rendering molecule: {e}")
        raise HTTPException(status_code=500, detail="Error rendering molecule")


@router.post("/utils/render-result")
async def render_result(request: RenderRequest):
    """Return a base64 SVG of the result molecule, highlighting BBs if provided."""
    try:
        smiles = request.smiles.strip()
        alpha = float(request.alpha)
        # get tuple from bgColor: 'rgba(235, 64, 52, 0.06)' -> (235, 64, 52)
        bg_color = request.bgColor.replace(' ', '').replace('rgba(', '').replace(')', '').split(',')
        bg_color = tuple(int(c)/255.0 for c in bg_color[:3])  # ignore alpha for bg color
        if request.bbs:
            valid_bbs = [bb for bb in request.bbs if bb and bb.strip()]
            if valid_bbs:
                try:
                    svg_data_uri = utils.get_svg_mol_with_bbs(
                        smiles, valid_bbs, legend="", alpha=alpha, bg_color_for_transparency=bg_color
                    )
                    return {"svg": svg_data_uri}
                except Exception as e:
                    print(f"Error highlighting BBs: {e}")
        
        svg_data_uri = utils.get_svg_mol(smiles, legend="")
        return {"svg": svg_data_uri}
    except Exception as e:
        try:
            svg_data_uri = utils.get_svg_mol(request.smiles, legend="")
            return {"svg": svg_data_uri}
        except Exception:
            print(f"Error rendering result: {e}")
            raise HTTPException(status_code=500, detail="Error rendering result")
