"""Regression checks for the Cloud Run deployment contract."""

from pathlib import Path


DEPLOY_SCRIPT = Path(__file__).parents[1] / "deploy" / "deploy-cloud-run.sh"


def test_web_startup_probe_allows_gcsfuse_cold_start():
    """A healthy app must have enough time to mount GCS FUSE and start Uvicorn.

    The previous 3 x 10-second probe budget rejected a new instance before the
    storage mount and application process had completed initialization.
    """
    deployment = DEPLOY_SCRIPT.read_text()

    assert "--startup-probe=httpGet.path=/api/health,httpGet.port=8080," in deployment
    assert "periodSeconds=10,failureThreshold=6" in deployment

