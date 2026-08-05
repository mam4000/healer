"""Shared Redis job state used by the API and request-driven worker."""
import json
import os
from typing import Any, Optional

from redis import Redis
from redis.exceptions import RedisError

REDIS_URL = os.environ.get("HEALER_REDIS_URL", "redis://localhost:6379/0")
RESULT_TTL_SECONDS = int(os.environ.get("HEALER_RESULT_TTL_SECONDS", "7200"))
KEY_PREFIX = "healer:job:"


class JobStoreUnavailableError(RuntimeError):
    pass


def _client() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _key(job_id: str) -> str:
    return f"{KEY_PREFIX}{job_id}"


def create(job_id: str, task_name: str) -> None:
    try:
        _client().set(_key(job_id), json.dumps({"status": "PENDING", "task_name": task_name}), ex=RESULT_TTL_SECONDS)
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


def delete(job_id: str) -> None:
    try:
        _client().delete(_key(job_id))
    except RedisError as exc:
        raise JobStoreUnavailableError("The job store is temporarily unavailable") from exc
