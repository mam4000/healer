"""
Smoke tests for healer.web.interface — building block discovery and path resolution.
"""
import os
from pathlib import Path

import pytest

import healer.web.interface as interface
from healer.web.interface import discover_building_blocks, resolve_bb_path, BB_BASE_PATH
from healer.domain.bb_repository import resolve_bb_path as repo_resolve_bb_path


# ---------------------------------------------------------------------------
# discover_building_blocks
# ---------------------------------------------------------------------------

def test_discover_returns_list():
    result = discover_building_blocks()
    assert isinstance(result, list)


def test_discover_finds_test_bb():
    """The bundled test_100_bb_processed.sdf should always be discoverable."""
    result = discover_building_blocks()
    # At minimum the package ships with the test SDF — at least one entry expected
    assert len(result) >= 1, (
        "discover_building_blocks returned nothing. "
        f"BB_BASE_PATH={BB_BASE_PATH} — does test_100_bb_processed.sdf exist there?"
    )


def test_discover_entry_shape():
    """Every entry must have 'value' and 'label' string fields."""
    result = discover_building_blocks()
    for entry in result:
        assert "value" in entry, f"Entry missing 'value': {entry}"
        assert "label" in entry, f"Entry missing 'label': {entry}"
        assert isinstance(entry["value"], str)
        assert isinstance(entry["label"], str)


def test_discover_values_are_absolute_paths():
    """All 'value' fields must be absolute paths that exist on disk."""
    result = discover_building_blocks()
    for entry in result:
        path = Path(entry["value"])
        assert path.is_absolute(), f"Non-absolute path in discover results: {path}"
        assert path.exists(), f"Path does not exist on disk: {path}"


def test_discover_test_entry_has_expected_label():
    """The test BB entry should carry the 'Test Set (100 BBs)' label."""
    result = discover_building_blocks()
    test_entries = [e for e in result if e.get("key") == "test"]
    if test_entries:
        assert test_entries[0]["label"] == "Test Set (100 BBs)"


def test_discover_groups_molport_shards_into_one_logical_source(tmp_path, monkeypatch):
    molport_dir = tmp_path / "Molport_Full_Database"
    molport_dir.mkdir()
    (molport_dir / "first_processed.sdf").touch()
    (molport_dir / "second_processed.sdf").touch()
    monkeypatch.setattr(interface, "BB_BASE_PATH", tmp_path)

    result = interface.discover_building_blocks()
    assert result == [{
        "value": "molport_full",
        "label": "Molport Full Database",
        "key": "molport_full",
    }]


# ---------------------------------------------------------------------------
# resolve_bb_path (interface layer)
# ---------------------------------------------------------------------------

def test_resolve_named_key_test():
    """'test' named key resolves to an existing SDF file."""
    path = resolve_bb_path("test")
    assert Path(path).exists(), f"Resolved path does not exist: {path}"
    assert path.endswith(".sdf")


def test_resolve_absolute_path(test_bb_path: str):
    """An absolute path that exists is returned as-is."""
    result = resolve_bb_path(test_bb_path)
    assert result == test_bb_path


def test_resolve_nonexistent_absolute_path():
    """An absolute path that does not exist raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        resolve_bb_path("/nonexistent/path/to/file.sdf")


def test_resolve_bad_key_raises():
    """An unknown key that is not an existing path raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        resolve_bb_path("this_key_does_not_exist_anywhere")


# ---------------------------------------------------------------------------
# resolve_bb_path (domain / bb_repository layer)
# ---------------------------------------------------------------------------

def test_repo_resolve_named_key_test():
    """'test' named key resolves via bb_repository layer to an existing SDF."""
    path = repo_resolve_bb_path("test")
    assert Path(path).exists()
    assert path.endswith(".sdf")


def test_repo_resolve_nonexistent_raises():
    with pytest.raises(FileNotFoundError):
        repo_resolve_bb_path("/does/not/exist.sdf")


def test_server_limits_cap_omitted_and_excessive_work(monkeypatch):
    """Shared server callers cannot bypass limits by omitting optional values."""
    monkeypatch.setattr(interface, "SERVER_MODE", True)
    limited = interface.apply_server_limits({
        "reaction_tags": ["amide", "C-N"],
        "max_evals_per_comp": None,
        "max_products_per_comp": 999999,
        "max_total_products": None,
        "sim_threshold": 0.0,
        "max_bbs_per_frag": -1,
        "n_compositions": 999,
        "retro_tree_depth": 99,
        "min_frag_size": 1,
    })

    assert limited["max_evals_per_comp"] == interface.SERVER_LIMITS["max_evals_per_comp"]
    assert limited["max_products_per_comp"] == interface.SERVER_LIMITS["max_products_per_comp"]
    assert limited["max_total_products"] == interface.SERVER_LIMITS["max_total_products"]
    assert limited["max_bbs_per_frag"] == interface.SERVER_LIMITS["max_bbs_per_frag"]
    assert limited["n_compositions"] == interface.SERVER_LIMITS["n_compositions_max"]
    assert limited["retro_tree_depth"] == interface.SERVER_LIMITS["retro_depth_max"]
    assert limited["min_frag_size"] == interface.SERVER_LIMITS["min_frag_size_min"]


def test_server_limits_reject_all_reaction_tag(monkeypatch):
    monkeypatch.setattr(interface, "SERVER_MODE", True)
    with pytest.raises(ValueError, match="unavailable"):
        interface.apply_server_limits({"reaction_tags": ["all"]})


def test_server_limits_turn_zero_max_bbs_into_a_bounded_value(monkeypatch):
    """Zero meant unlimited in the legacy UI but is unsafe for Molport."""
    monkeypatch.setattr(interface, "SERVER_MODE", True)
    limited = interface.apply_server_limits({
        "reaction_tags": ["amide"],
        "max_bbs_per_frag": 0,
    })
    assert limited["max_bbs_per_frag"] == interface.SERVER_LIMITS["max_bbs_per_frag"]
