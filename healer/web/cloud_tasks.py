"""Cloud Tasks submission for on-demand Cloud Run enumeration workers."""
import json
import os
from typing import Any

from google.cloud import tasks_v2
from google.protobuf import duration_pb2
from google.api_core.exceptions import AlreadyExists, NotFound

PROJECT_ID = os.environ.get("HEALER_GCP_PROJECT", "")
LOCATION = os.environ.get("HEALER_TASKS_LOCATION", "")
QUEUE_NAME = os.environ.get("HEALER_TASKS_QUEUE", "healer-enumeration")
WORKER_URL = os.environ.get("HEALER_TASK_WORKER_URL", "").rstrip("/")
DISPATCHER_SERVICE_ACCOUNT = os.environ.get("HEALER_TASK_DISPATCHER_SERVICE_ACCOUNT", "")


class TaskSubmissionError(RuntimeError):
    pass


TASK_DEADLINE_SECONDS = int(os.environ.get("HEALER_TASK_DEADLINE_SECONDS", "900"))


def _create_task(task_id: str, path: str, payload_data: dict[str, Any]) -> str:
    if not all((PROJECT_ID, LOCATION, WORKER_URL, DISPATCHER_SERVICE_ACCOUNT)):
        raise TaskSubmissionError("Cloud Tasks is not configured")
    try:
        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
        task_name = client.task_path(PROJECT_ID, LOCATION, QUEUE_NAME, task_id)
        payload = json.dumps(payload_data).encode()
        task = {
            "name": task_name,
            "dispatch_deadline": duration_pb2.Duration(seconds=TASK_DEADLINE_SECONDS),
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{WORKER_URL}{path}",
                "headers": {"Content-Type": "application/json"},
                "body": payload,
                "oidc_token": {
                    "service_account_email": DISPATCHER_SERVICE_ACCOUNT,
                    "audience": WORKER_URL,
                },
            },
        }
        created = client.create_task(request={"parent": parent, "task": task})
        return created.name
    except AlreadyExists:
        # A retry after Cloud Tasks accepted the request is already queued.
        return task_name
    except Exception as exc:
        raise TaskSubmissionError("Unable to queue the enumeration job") from exc


def create_enumeration_task(job_id: str, job_type: str, params: dict[str, Any]) -> str:
    return _create_task(job_id, f"/internal/tasks/enumerate/{job_type}", {"job_id": job_id, "params": params})


def molport_shard_task_name_for(job_id: str, shard_id: str) -> str:
    return task_name_for(f"{job_id}-shard-{shard_id}")


def molport_merge_task_name_for(job_id: str) -> str:
    return task_name_for(f"{job_id}-merge")


def create_molport_shard_task(job_id: str, shard_id: str, shard_name: str, params: dict[str, Any]) -> str:
    return _create_task(
        f"{job_id}-shard-{shard_id}",
        "/internal/tasks/molport-shard",
        {"job_id": job_id, "shard_id": shard_id, "shard_name": shard_name, "params": params},
    )


def create_molport_merge_task(job_id: str, params: dict[str, Any]) -> str:
    return _create_task(
        f"{job_id}-merge", "/internal/tasks/molport-merge", {"job_id": job_id, "params": params}
    )


def task_name_for(job_id: str) -> str:
    if not all((PROJECT_ID, LOCATION)):
        raise TaskSubmissionError("Cloud Tasks is not configured")
    return tasks_v2.CloudTasksClient.task_path(PROJECT_ID, LOCATION, QUEUE_NAME, job_id)


def delete_task(task_name: str) -> None:
    try:
        tasks_v2.CloudTasksClient().delete_task(request={"name": task_name})
    except NotFound:
        return
    except Exception as exc:
        raise TaskSubmissionError("Unable to cancel the queued job") from exc
