"""Cloud Tasks submission for on-demand Cloud Run enumeration workers."""
import json
import os
from typing import Any

from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

PROJECT_ID = os.environ.get("HEALER_GCP_PROJECT", "")
LOCATION = os.environ.get("HEALER_TASKS_LOCATION", "")
QUEUE_NAME = os.environ.get("HEALER_TASKS_QUEUE", "healer-enumeration")
WORKER_URL = os.environ.get("HEALER_TASK_WORKER_URL", "").rstrip("/")
DISPATCHER_SERVICE_ACCOUNT = os.environ.get("HEALER_TASK_DISPATCHER_SERVICE_ACCOUNT", "")


class TaskSubmissionError(RuntimeError):
    pass


def create_enumeration_task(job_id: str, job_type: str, params: dict[str, Any]) -> str:
    if not all((PROJECT_ID, LOCATION, WORKER_URL, DISPATCHER_SERVICE_ACCOUNT)):
        raise TaskSubmissionError("Cloud Tasks is not configured")
    try:
        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
        task_name = client.task_path(PROJECT_ID, LOCATION, QUEUE_NAME, job_id)
        payload = json.dumps({"job_id": job_id, "params": params}).encode()
        task = {
            "name": task_name,
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{WORKER_URL}/internal/tasks/enumerate/{job_type}",
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
    except Exception as exc:
        raise TaskSubmissionError("Unable to queue the enumeration job") from exc


def task_name_for(job_id: str) -> str:
    if not all((PROJECT_ID, LOCATION)):
        raise TaskSubmissionError("Cloud Tasks is not configured")
    return tasks_v2.CloudTasksClient.task_path(PROJECT_ID, LOCATION, QUEUE_NAME, job_id)


def delete_task(task_name: str) -> None:
    try:
        tasks_v2.CloudTasksClient().delete_task(request={"name": task_name})
    except Exception as exc:
        raise TaskSubmissionError("Unable to cancel the queued job") from exc
