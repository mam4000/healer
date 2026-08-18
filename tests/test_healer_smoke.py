"""
Smoke tests for MoleculeHEALER and SiteHEALER end-to-end pipelines.

Design philosophy:
  - Use the bundled 100-BB test set and reaction_tags='all' to maximise the
    chance of actual chemistry happening with a small library.
  - sim_threshold=0.0 so no BB is rejected by fingerprint similarity — with
    100 BBs a similarity filter would almost certainly return nothing.
  - max_total_products=10 keeps runtime short; we only need to prove the
    pipeline returns valid output, not that it enumerates everything.
  - The query molecule (row 0) is always included in enumerated_molecules, so
    get_results() always returns ≥1 row even if no reactions fire.
  - We assert ≥1 row and valid SMILES for every row; we separately assert
    >1 rows to confirm actual enumeration happened (penicillin has amide bonds
    that are well-covered by the default reaction set).
"""
import pytest
from rdkit import Chem

from healer.application.healer import MoleculeHEALER, SiteHEALER
from healer.domain.bb_repository import BBRepository
from tests.conftest import PENICILLIN_SMILES, ASPIRIN_SMILES


# ---------------------------------------------------------------------------
# MoleculeHEALER
# ---------------------------------------------------------------------------

def test_composition_prints_handles_no_compositions():
    healer = object.__new__(MoleculeHEALER)
    healer._compositions = []
    assert healer._composition_prints() == "No compositions found."

@pytest.fixture(scope="module")
def molecule_healer(test_bb_repository: BBRepository) -> MoleculeHEALER:
    """
    Single MoleculeHEALER instance shared across the module.
    - reaction_tags='all': use every valid reaction so something fires.
    - sim_threshold=0.0: accept every BB regardless of similarity.
    - max_bbs_per_frag=-1: do not cap the BB pool.
    Injecting test_bb_repository avoids reloading from disk.
    """
    return MoleculeHEALER(
        bb_source="test",
        reaction_tags="all",
        bb_repository=test_bb_repository,
        sim_threshold=0.0,
        max_bbs_per_frag=10,
        verbose=0,
    )


def test_molecule_healer_initializes(molecule_healer: MoleculeHEALER):
    """HEALER object initialises and reactions are loaded."""
    assert len(molecule_healer.reactions) > 0, "No reactions loaded"
    assert molecule_healer._bb_repo.is_loaded


def test_molecule_healer_has_building_blocks(molecule_healer: MoleculeHEALER):
    """bb_mols returns at least one building block compatible with the reactions."""
    bbs = molecule_healer.bb_mols
    assert len(bbs) > 0, "No building blocks found compatible with the reactions"


def test_molecule_healer_pipeline(molecule_healer: MoleculeHEALER):
    """
    Full pipeline: set_query_mol → enumerate → get_results returns valid output.

    retro_tree_depth=1 keeps it to a single split — fastest path.
    min_frag_size=3 allows small fragments so the test BB library can reach them.
    max_total_products=10 caps enumeration so the test finishes quickly.
    """
    molecule_healer.set_query_mol(
        query_mol=PENICILLIN_SMILES,
        n_compositions=10,
        retro_tree_depth=1,
        min_frag_size=3,
    )
    molecule_healer.enumerate(max_total_products=10)

    results = molecule_healer.get_results(
        as_dict=True,
        calc_similarity=True,
        calc_properties=False,   # skip slow property profiling in CI
    )

    # Always ≥1 row: query mol is row 0 by construction
    assert len(results) >= 1, "get_results returned an empty list"

    # Every row must have a 'Product' key with a parseable SMILES
    for row in results:
        assert "Product" in row, f"Row missing 'Product' key: {row.keys()}"
        mol = Chem.MolFromSmiles(row["Product"])
        assert mol is not None, f"Invalid product SMILES: {row['Product']}"


def test_molecule_healer_produces_enumerated_products(molecule_healer: MoleculeHEALER):
    """
    Verify that at least one enumerated product (beyond the query mol) is produced.
    Penicillin has a clear amide bond covered by the default reaction set, so this
    should reliably fire with reaction_tags='all' and sim_threshold=0.0.
    """
    assert len(molecule_healer.enumerated_molecules) > 1, (
        "Only the query molecule was returned — no reactions fired. "
        "Check that the test BB SDF contains amines or acids compatible with penicillin fragments."
    )


def test_molecule_healer_similarity_column(molecule_healer: MoleculeHEALER):
    """When calc_similarity=True, every row has a numeric Similarity_to_query."""
    results = molecule_healer.get_results(
        as_dict=True,
        calc_similarity=True,
        calc_properties=False,
    )
    for row in results:
        assert "Similarity_to_query" in row, "Missing Similarity_to_query column"
        assert isinstance(row["Similarity_to_query"], float)


def test_molecule_healer_get_results_as_dataframe(molecule_healer: MoleculeHEALER):
    """get_results(as_dict=False) returns a DataFrame with expected columns."""
    import pandas as pd
    df = molecule_healer.get_results(as_dict=False, calc_similarity=False, calc_properties=False)
    assert isinstance(df, pd.DataFrame)
    assert "Product" in df.columns
    assert "ID" in df.columns


def test_molecule_healer_reruns_on_different_mol(test_bb_repository: BBRepository):
    """
    set_query_mol can be called again on the same instance without reinitialising.
    Uses aspirin, which is small and fast.
    """
    healer = MoleculeHEALER(
        bb_source="test",
        reaction_tags="all",
        bb_repository=test_bb_repository,
        sim_threshold=0.0,
        max_bbs_per_frag=10,
        verbose=0,
    )
    healer.set_query_mol(ASPIRIN_SMILES, n_compositions=5, retro_tree_depth=1, min_frag_size=3)
    healer.enumerate(max_total_products=5)
    results = healer.get_results(as_dict=True, calc_similarity=False, calc_properties=False)
    assert len(results) >= 1

    # Re-use the same instance
    healer.set_query_mol(PENICILLIN_SMILES, n_compositions=5, retro_tree_depth=1, min_frag_size=3)
    healer.enumerate(max_total_products=5)
    results2 = healer.get_results(as_dict=True, calc_similarity=False, calc_properties=False)
    assert len(results2) >= 1


# ---------------------------------------------------------------------------
# SiteHEALER
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def site_healer(test_bb_repository: BBRepository) -> SiteHEALER:
    """
    SiteHEALER with very permissive property rules to ensure BBs pass the filter.
    """
    return SiteHEALER(
        bb_source="test",
        reaction_tags="all",
        bb_repository=test_bb_repository,
        rules={
            "MW":      (0, 1000),
            "HBD":     (0, 10),
            "HBA":     (0, 20),
            "TPSA":    (0, 500),
            "RotB":    (0, 20),
            "Rings":   (0, 20),
            "ArRings": (0, 10),
            "Chiral":  (0, 10),
        },
        verbose=0,
    )


def test_site_healer_initializes(site_healer: SiteHEALER):
    assert len(site_healer.reactions) > 0
    assert site_healer._bb_repo.is_loaded


def test_site_healer_pipeline(site_healer: SiteHEALER):
    """
    Full SiteHEALER pipeline: set_query_mol → enumerate → get_results.
    No reactive_sites specified so all atoms are considered reactive — this
    maximises the chance of a reaction firing with the small test library.
    """
    site_healer.set_query_mol(query_mol=PENICILLIN_SMILES)
    site_healer.enumerate(max_total_products=10)

    results = site_healer.get_results(
        as_dict=True,
        calc_similarity=True,
        calc_properties=False,
    )

    assert len(results) >= 1
    for row in results:
        assert "Product" in row
        mol = Chem.MolFromSmiles(row["Product"])
        assert mol is not None, f"Invalid product SMILES: {row['Product']}"


def test_site_healer_id_column(site_healer: SiteHEALER):
    """Every result row has a zero-padded numeric identifier."""
    results = site_healer.get_results(as_dict=True, calc_similarity=False, calc_properties=False)
    for row in results:
        assert "ID" in row
        assert row["ID"].isdigit()
        assert len(row["ID"]) == 6
