"""
Unit tests for healer.domain.bb_repository — path resolution and repository loading.
"""
import os
from pathlib import Path

import pytest

from healer.domain.bb_repository import (
    _build_bb_paths,
    resolve_bb_path,
    get_repository,
    BBRepository,
    ShardedBBRepository,
)


# ---------------------------------------------------------------------------
# _build_bb_paths
# ---------------------------------------------------------------------------

def test_build_bb_paths_returns_expected_keys():
    paths = _build_bb_paths()
    assert isinstance(paths, dict)
    for key in ("US_stock", "EU_stock", "Global_stock", "test"):
        assert key in paths, f"Expected key '{key}' missing from _build_bb_paths()"


def test_build_bb_paths_values_are_strings():
    for key, val in _build_bb_paths().items():
        assert isinstance(val, str), f"Path for '{key}' is not a string: {val!r}"


def test_build_bb_paths_test_key_is_not_glob():
    """The 'test' key points to a single file, not a glob pattern."""
    path = _build_bb_paths()["test"]
    assert "*" not in path and "?" not in path and "[" not in path


def test_build_bb_paths_stock_keys_are_globs():
    """The stock keys use a glob pattern so they match any dated filename."""
    paths = _build_bb_paths()
    for key in ("US_stock", "EU_stock", "Global_stock"):
        assert "*" in paths[key], f"Expected glob pattern for '{key}'"


# ---------------------------------------------------------------------------
# resolve_bb_path — named keys
# ---------------------------------------------------------------------------

def test_resolve_test_key_returns_existing_file():
    path = resolve_bb_path("test")
    assert Path(path).exists(), f"Resolved path does not exist: {path}"
    assert path.endswith(".sdf")


def test_resolve_test_key_is_absolute():
    path = resolve_bb_path("test")
    assert Path(path).is_absolute()


# ---------------------------------------------------------------------------
# resolve_bb_path — direct paths
# ---------------------------------------------------------------------------

def test_resolve_absolute_existing_path(test_bb_path: str):
    """An absolute path that exists on disk is returned as-is."""
    result = resolve_bb_path(test_bb_path)
    assert result == test_bb_path


def test_resolve_nonexistent_absolute_raises():
    with pytest.raises(FileNotFoundError):
        resolve_bb_path("/this/path/does/not/exist.sdf")


def test_resolve_unknown_key_raises():
    """A string that is not a named key and does not exist as a path raises."""
    with pytest.raises(FileNotFoundError):
        resolve_bb_path("completely_unknown_key_xyz")


# ---------------------------------------------------------------------------
# get_repository / BBRepository
# ---------------------------------------------------------------------------

def test_get_repository_returns_bb_repository(test_bb_path: str):
    repo = get_repository(test_bb_path)
    assert isinstance(repo, BBRepository)


def test_repository_loads_building_blocks(test_bb_repository: BBRepository):
    assert test_bb_repository.is_loaded
    assert len(test_bb_repository) > 0


def test_repository_cache_returns_same_object(test_bb_path: str):
    """Calling get_repository twice with the same path returns the same cached object."""
    repo1 = get_repository(test_bb_path)
    repo2 = get_repository(test_bb_path)
    assert repo1 is repo2


def test_sharded_repository_streams_one_processed_sdf_at_a_time(tmp_path, caplog):
    """The sharded repository filters while iterating and never needs load()."""
    from rdkit import Chem

    shard = tmp_path / "part_processed.sdf"
    writer = Chem.SDWriter(str(shard))
    compatible = Chem.MolFromSmiles("CCN")
    compatible.SetProp("rxn_annotations", '{"wanted": []}')
    incompatible = Chem.MolFromSmiles("CCO")
    incompatible.SetProp("rxn_annotations", '{"other": []}')
    writer.write(compatible)
    writer.write(incompatible)
    writer.close()

    class Reaction:
        name = "wanted"

    repo = ShardedBBRepository(str(tmp_path))
    bbs = list(repo.iter_bbs_for_reactions([Reaction()]))
    assert repo.is_loaded
    assert repo.loaded_count == 0
    assert len(bbs) == 1
    assert bbs[0].get_smiles() == "CCN"
    assert "Molport shard 1/1 started" in caplog.text
    assert "Molport shard 1/1 completed" in caplog.text
    assert "scanned=2, compatible=1" in caplog.text


def test_repository_get_bbs_for_all_reactions(test_bb_repository: BBRepository):
    """BBs can be retrieved for all loaded reactions without error."""
    from healer.utils import utils
    from healer.domain.bb_repository import _DATA_DIR

    reactions_path = _DATA_DIR / "reactions" / "reactions.json"
    all_rxns = utils.load_reactions_from_json(str(reactions_path))
    valid_rxns = [r for r in all_rxns if r.is_valid()]

    bbs = test_bb_repository.get_bbs_for_reactions(valid_rxns)
    # Not asserting a specific count — just that the call succeeds and returns a list
    assert isinstance(bbs, list)
