"""Shared Redis job state used by the API and request-driven worker."""
import json
import os
from typing import Any, Optional

from redis import Redis
from redis.exceptions import RedisError, WatchError

REDIS_URL = os.environ.get("HEALER_REDIS_URL", "redis://localhost:6379/0")
RESULT_TTL_SECONDS = int(os.environ.get("HEALER_RESULT_TTL_SECONDS", "7200"))
KEY_PREFIX = "healer:job:"


class JobStoreUnavailableError(RuntimeError):
    pass


def _client() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _key(job_id: str) -> str:
    return f"{KEY_PREFIX}{job_id}"


def create(job_id: str, task_name: str, owner: str) -> None:
    try:
        _client().set(_key(job_id), json.dumps({"status": "PENDING", "task_name": task_name, "owner": owner}), ex=RESULT_TTL_SECONDS)
    except RedisError as exc:
        raise JobStoreUnavailableError("The job store is temporarily unavailable") from exc


def create_fanout(
    job_id: str, shard_tasks: dict[str, str], merge_task_name: str, params: dict[str, Any], owner: str
) -> None:
    """Create the parent state for a bounded Molport fan-out job."""
    job = {
        "status": "PENDING",
        "phase": "SCANNING",
        "task_names": shard_tasks,
        "merge_task_name": merge_task_name,
        # Retain the already server-limited request for safe task recovery
        # during the same two-hour result lifetime.
        "params": params,
        "total_shards": len(shard_tasks),
        "completed_shards": [],
        "shard_results": {},
        "merge_claimed": False,
        "owner": owner,
    }
    try:
        _client().set(_key(job_id), json.dumps(job), ex=RESULT_TTL_SECONDS)
    except RedisError as exc:
        raise JobStoreUnavailableError("The job store is temporarily unavailable") from exc


def get(job_id: str) -> Optional[dict[str, Any]]:
    try:
        raw = _client().get(_key(job_id))
        return json.loads(raw) if raw else None
    except RedisError as exc:
        raise JobStoreUnavailableError("The job store is temporarily unavailable") from exc


def update(job_id: str, **changes: Any) -> None:
    job = get(job_id)
    if job is None:
        return
    job.update(changes)
    try:
        _client().set(_key(job_id), json.dumps(job), ex=RESULT_TTL_SECONDS)
    except RedisError as exc:
        raise JobStoreUnavailableError("The job store is temporarily unavailable") from exc


def _mutate(job_id: str, mutate: Any) -> Any:
    """Atomically update one JSON job document and return a mutation result."""
    client = _client()
    try:
        while True:
            with client.pipeline() as pipe:
                try:
                    pipe.watch(_key(job_id))
                    raw = pipe.get(_key(job_id))
                    if raw is None:
                        return None
                    job = json.loads(raw)
                    result = mutate(job)
                    pipe.multi()
                    pipe.set(_key(job_id), json.dumps(job), ex=RESULT_TTL_SECONDS)
                    pipe.execute()
                    return result
                except WatchError:
                    continue
    except RedisError as exc:
        raise JobStoreUnavailableError("The job store is temporarily unavailable") from exc


def record_shard_success(job_id: str, shard_id: str, result: Any) -> bool:
    """Save a partial result and claim the merge task once all shards finish.

    Returns true only to the request that must enqueue the merge task.
    """
    def mutate(job: dict[str, Any]) -> bool:
        if job.get("status") in {"CANCELLED", "FAILURE", "SUCCESS"}:
            return False
        job["status"] = "STARTED"
        job["phase"] = "SCANNING"
        job.setdefault("shard_results", {})[shard_id] = result
        completed = set(job.setdefault("completed_shards", []))
        completed.add(shard_id)
        job["completed_shards"] = sorted(completed)
        if len(completed) == job.get("total_shards", 0) and not job.get("merge_claimed"):
            job["merge_claimed"] = True
            job["phase"] = "MERGING"
            return True
        return False
    return bool(_mutate(job_id, mutate))


def record_shard_failure(job_id: str, shard_id: str) -> None:
    def mutate(job: dict[str, Any]) -> None:
        if job.get("status") != "CANCELLED":
            job.update(status="FAILURE", phase="FAILED", failed_shard=shard_id)
    _mutate(job_id, mutate)


def delete(job_id: str) -> None:
    try:
        _client().delete(_key(job_id))
    except RedisError as exc:
        raise JobStoreUnavailableError("The job store is temporarily unavailable") from exc
