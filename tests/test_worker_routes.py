"""Focused tests for the request-driven Molport fan-out task handlers."""
import asyncio

from healer.web import worker_routes


def test_last_molport_shard_queues_one_merge(monkeypatch):
    queued = []
    monkeypatch.setattr(worker_routes.job_store, "get", lambda _: {"status": "STARTED"})
    monkeypatch.setattr(worker_routes, "run_molport_shard_selection", lambda *_: [[{"smiles": "CC", "score": 1.0, "props": {}}]])
    monkeypatch.setattr(worker_routes.job_store, "record_shard_success", lambda *_: True)
    monkeypatch.setattr(worker_routes, "create_molport_merge_task", lambda *args: queued.append(args))

    request = worker_routes.MolportShardTask(
        job_id="job-1", shard_id="12", shard_name="part_processed.sdf", params={"bb_source": "molport_full"}
    )
    response = asyncio.run(worker_routes.molport_shard_task(request))

    assert response["status"] == "complete"
    assert queued == [("job-1", {"bb_source": "molport_full"})]


def test_cancelled_molport_shard_does_not_scan(monkeypatch):
    monkeypatch.setattr(worker_routes.job_store, "get", lambda _: {"status": "CANCELLED"})
    monkeypatch.setattr(
        worker_routes,
        "run_molport_shard_selection",
        lambda *_: (_ for _ in ()).throw(AssertionError("cancelled work must not scan")),
    )

    request = worker_routes.MolportShardTask(
        job_id="job-1", shard_id="0", shard_name="part_processed.sdf", params={}
    )
    response = asyncio.run(worker_routes.molport_shard_task(request))

    assert response["status"] == "ignored"
