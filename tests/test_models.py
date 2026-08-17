"""
Unit tests for healer.web.models — Pydantic schema validation.

These tests verify that:
  - Valid payloads are accepted and defaults are set correctly.
  - Required fields are enforced.
  - Serialization round-trips are lossless.
"""
import pytest
from pydantic import ValidationError

from healer.web.models import (
    MoleculeRequest,
    SiteRequest,
    JobSubmitResponse,
    JobStatusResponse,
    JobResult,
)


# ---------------------------------------------------------------------------
# MoleculeRequest
# ---------------------------------------------------------------------------

def test_molecule_request_minimal():
    """Only 'molecule' is required; all other fields take their defaults."""
    req = MoleculeRequest(molecule="CCO")
    assert req.molecule == "CCO"
    assert req.bb_source == "molport_full"
    assert req.sim_threshold == 0.15
    assert req.retro_tree_depth == 1
    assert req.min_frag_size == 3
    assert req.max_bbs_per_frag == -1
    assert req.use_fragment_healer is False


def test_molecule_request_custom_values():
    req = MoleculeRequest(
        molecule="CC(=O)Oc1ccccc1C(=O)O",
        bb_source="US_stock",
        reaction_tags=["amide coupling"],
        sim_threshold=0.3,
        n_compositions=5,
        retro_tree_depth=2,
        min_frag_size=6,
        max_bbs_per_frag=20,
        shuffle_bb_order=True,
        use_fragment_healer=True,
    )
    assert req.bb_source == "US_stock"
    assert req.reaction_tags == ["amide coupling"]
    assert req.sim_threshold == 0.3
    assert req.n_compositions == 5
    assert req.retro_tree_depth == 2
    assert req.max_bbs_per_frag == 20
    assert req.use_fragment_healer is True


def test_molecule_request_missing_molecule():
    """molecule is required; omitting it should raise ValidationError."""
    with pytest.raises(ValidationError):
        MoleculeRequest()


def test_molecule_request_serializes():
    """model_dump() includes all expected keys."""
    req = MoleculeRequest(molecule="CCO")
    data = req.model_dump()
    expected_keys = {
        "molecule", "bb_source", "reaction_tags", "custom_sites",
        "sim_threshold", "n_compositions", "randomize_compositions",
        "random_seed", "retro_tree_depth", "min_frag_size",
        "max_bbs_per_frag", "shuffle_bb_order",
        "max_evals_per_comp", "max_products_per_comp", "max_total_products",
        "use_fragment_healer",
    }
    assert expected_keys.issubset(data.keys())


def test_molecule_request_custom_sites_optional():
    """custom_sites defaults to None and accepts a list of tuples."""
    req = MoleculeRequest(molecule="CCO")
    assert req.custom_sites is None

    req2 = MoleculeRequest(molecule="CCO", custom_sites=[(0, 1)])
    assert req2.custom_sites == [(0, 1)]


# ---------------------------------------------------------------------------
# SiteRequest
# ---------------------------------------------------------------------------

def test_site_request_minimal():
    req = SiteRequest(molecule="CCO")
    assert req.molecule == "CCO"
    assert req.bb_source == "molport_full"
    assert req.reactive_sites is None
    assert req.rules is not None  # default rules are set


def test_site_request_missing_molecule():
    with pytest.raises(ValidationError):
        SiteRequest()


def test_site_request_custom_rules():
    req = SiteRequest(
        molecule="CCO",
        rules={"MW": (0, 300), "HBD": (0, 3), "HBA": (0, 5),
               "TPSA": (0, 100), "RotB": (0, 5), "Rings": (0, 5),
               "ArRings": (0, 3), "Chiral": (0, 2)},
    )
    assert req.rules["MW"] == (0, 300)


def test_site_request_struct_rules_default():
    req = SiteRequest(molecule="CCO")
    assert req.struct_rules == []


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

def test_job_submit_response():
    resp = JobSubmitResponse(job_id="abc-123", status="submitted")
    assert resp.job_id == "abc-123"
    assert resp.status == "submitted"


def test_job_status_response_pending():
    resp = JobStatusResponse(job_id="abc-123", status="PENDING")
    assert resp.result is None


def test_job_status_response_success():
    result = JobResult(display=[{"Product": "CCO"}], complete=[{"Product": "CCO"}])
    resp = JobStatusResponse(job_id="abc-123", status="SUCCESS", result=result)
    assert resp.result is not None
    assert resp.result.display[0]["Product"] == "CCO"


def test_job_result_round_trip():
    """JobResult serialises and deserialises cleanly."""
    result = JobResult(
        display=[{"Product": "CCO", "Similarity_to_query": 0.9}],
        complete=[{"Product": "CCO", "BB1": "CCN", "Reaction1_name": "amide coupling"}],
    )
    data = result.model_dump()
    restored = JobResult(**data)
    assert restored.display == result.display
    assert restored.complete == result.complete
