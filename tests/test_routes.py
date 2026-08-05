"""
API route tests using FastAPI's TestClient (no server, no Redis/Celery needed).

All tests run in LOCAL mode: HEALER_SERVER_MODE is not set / set to 'false',
so jobs run synchronously and results are available immediately after the POST.
"""
import os
import asyncio
import pytest

# Force local mode before any healer imports so the routes module sees the
# correct value at import time.
os.environ.setdefault("HEALER_SERVER_MODE", "false")

from fastapi.testclient import TestClient
from fastapi import HTTPException

from healer.web.app import app
import healer.web.routes as routes
from healer.web.models import MoleculeRequest

# ---------------------------------------------------------------------------
# Shared client fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Utility / info endpoints (fast, no chemistry)
# ---------------------------------------------------------------------------

def test_health_check(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_get_building_blocks(client: TestClient):
    resp = client.get("/api/info/building-blocks")
    assert resp.status_code == 200
    body = resp.json()
    assert "building_blocks" in body
    assert isinstance(body["building_blocks"], list)
    # Must find at least the bundled test SDF
    assert len(body["building_blocks"]) >= 1


def test_get_limits(client: TestClient):
    resp = client.get("/api/info/limits")
    assert resp.status_code == 200
    body = resp.json()
    assert "limits" in body
    assert "server_mode" in body


def test_get_mode(client: TestClient):
    resp = client.get("/api/info/mode")
    assert resp.status_code == 200
    assert "mode" in resp.json()


# ---------------------------------------------------------------------------
# Molecule enumeration — success path
# ---------------------------------------------------------------------------

def test_molecule_enumeration_submit_and_poll(client: TestClient):
    """
    POST a valid molecule and confirm we get a completed job with results.

    Key choices:
    - bb_source='test': uses the bundled 100-BB SDF, always available.
    - reaction_tags=['all']: maximise chance of reactions firing.
    - sim_threshold=0.0: accept all BBs regardless of similarity.
    - max_total_products=5: fast, we just need to confirm the pipeline works.
    - retro_tree_depth=1: single split, fastest path.
    - min_frag_size=3: allow small fragments reachable by the test BB library.
    """
    payload = {
        "molecule": "CC1(C)SC2C(NC(=O)Cc3ccccc3)C(=O)N2C1C(=O)O",  # penicillin
        "bb_source": "test",
        "reaction_tags": ["all"],
        "sim_threshold": 0.0,
        "max_bbs_per_frag": 10,
        "retro_tree_depth": 1,
        "min_frag_size": 3,
        "n_compositions": 5,
        "max_total_products": 5,
    }
    submit_resp = client.post("/api/enumerate/molecule", json=payload)
    assert submit_resp.status_code == 200, submit_resp.text
    submit_body = submit_resp.json()
    assert "job_id" in submit_body
    job_id = submit_body["job_id"]

    # In local mode the job is synchronous — poll immediately
    poll_resp = client.get(f"/api/jobs/{job_id}")
    assert poll_resp.status_code == 200, poll_resp.text
    poll_body = poll_resp.json()

    assert poll_body["status"] == "SUCCESS", (
        f"Job ended with status {poll_body['status']}. "
        f"Check server logs for errors."
    )
    assert poll_body["result"] is not None
    assert "display" in poll_body["result"]
    assert "complete" in poll_body["result"]
    # At minimum the query molecule itself is always returned
    assert len(poll_body["result"]["complete"]) >= 1


# ---------------------------------------------------------------------------
# Molecule enumeration — error handling
# ---------------------------------------------------------------------------

def test_molecule_enumeration_invalid_smiles(client: TestClient):
    """
    Submitting an invalid SMILES string should result in a FAILURE job status,
    not a server crash.
    """
    payload = {
        "molecule": "THIS_IS_NOT_A_SMILES",
        "bb_source": "test",
        "reaction_tags": ["amide coupling"],
        "sim_threshold": 0.0,
        "max_total_products": 1,
    }
    submit_resp = client.post("/api/enumerate/molecule", json=payload)
    assert submit_resp.status_code == 200, submit_resp.text
    job_id = submit_resp.json()["job_id"]

    poll_resp = client.get(f"/api/jobs/{job_id}")
    assert poll_resp.status_code == 200
    assert poll_resp.json()["status"] == "FAILURE"


def test_job_not_found(client: TestClient):
    """Polling a non-existent job ID returns 404."""
    resp = client.get("/api/jobs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Site enumeration — success path
# ---------------------------------------------------------------------------

def test_site_enumeration_submit_and_poll(client: TestClient):
    """
    POST a valid site enumeration request and confirm successful completion.
    Very permissive rules so the 100-BB test library passes the property filter.
    """
    payload = {
        "molecule": "CC(=O)Oc1ccccc1C(=O)O",  # aspirin
        "bb_source": "test",
        "reaction_tags": ["all"],
        "rules": {
            "MW":      [0, 1000],
            "HBD":     [0, 10],
            "HBA":     [0, 20],
            "TPSA":    [0, 500],
            "RotB":    [0, 20],
            "Rings":   [0, 20],
            "ArRings": [0, 10],
            "Chiral":  [0, 10],
        },
        "max_total_products": 5,
    }
    submit_resp = client.post("/api/enumerate/site", json=payload)
    assert submit_resp.status_code == 200, submit_resp.text
    job_id = submit_resp.json()["job_id"]

    poll_resp = client.get(f"/api/jobs/{job_id}")
    assert poll_resp.status_code == 200
    body = poll_resp.json()
    assert body["status"] == "SUCCESS", f"Job failed: {body}"
    assert len(body["result"]["complete"]) >= 1


# ---------------------------------------------------------------------------
# Cancel endpoint (local mode)
# ---------------------------------------------------------------------------

def test_cancel_nonexistent_job_local_mode(client: TestClient):
    """In local mode, cancelling a nonexistent job returns 400."""
    resp = client.post("/api/jobs/nonexistent-job-id/cancel")
    assert resp.status_code == 400


def test_server_submission_applies_limits_before_task_creation(monkeypatch):
    """The asynchronous path limits the payload before creating a Cloud Task."""
    captured = {}

    def fake_create(job_id, job_type, params):
        captured["job_id"] = job_id
        captured["job_type"] = job_type
        captured.update(params)

    monkeypatch.setattr(routes, "USE_CELERY", True)
    monkeypatch.setattr(routes, "task_name_for", lambda job_id: f"tasks/{job_id}")
    monkeypatch.setattr(routes.job_store, "create", lambda *args: None)
    monkeypatch.setattr(routes, "create_enumeration_task", fake_create)
    monkeypatch.setattr(routes, "apply_server_limits", lambda params, _: {
        **params,
        "max_total_products": 500,
    })

    response = asyncio.run(routes.submit_molecule_enumeration(MoleculeRequest(
        molecule="CCO", reaction_tags=["amide"], max_total_products=None,
    )))

    assert response.job_id == captured["job_id"]
    assert captured["max_total_products"] == 500
