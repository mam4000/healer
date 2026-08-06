"""Focused tests for the request-driven Molport fan-out task handlers."""
import asyncio

from healer.web import worker_routes
from healer.web import interface


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


def test_merge_keeps_global_top_k_with_deterministic_ties(monkeypatch):
    captured = {}

    class Healer:
        def enumerate_from_molport_candidates(self, candidates, **kwargs):
            captured["candidates"] = candidates

        def get_results(self, **kwargs):
            return [{"Product": "CC"}]

    monkeypatch.setattr(interface, "_create_molport_molecule_healer", lambda _: Healer())
    result = interface.run_molport_merge(
        {"max_bbs_per_frag": 2},
        [
            [[{"smiles": "CCC", "score": 0.8, "props": {}}, {"smiles": "CC", "score": 0.5, "props": {}}]],
            [[{"smiles": "CO", "score": 0.9, "props": {}}, {"smiles": "C", "score": 0.8, "props": {}}]],
        ],
    )

    assert [item["smiles"] for item in captured["candidates"][0]] == ["CO", "C"]
    assert result == [{"Product": "CC"}]
