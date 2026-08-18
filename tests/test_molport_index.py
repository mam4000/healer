import os

import numpy as np
import pytest
from rdkit import Chem

from healer.domain.molport_index import MolportShardIndex, build_shard_index, index_dir_for
from healer.application.healer import MoleculeHEALER
from healer.utils.fingerprints import get_fingerprint_generator
from healer.utils.utils import get_batch_tversky_sims


def _write_shard(path):
    writer = Chem.SDWriter(str(path))
    for smiles, annotations, label in (
        ("CCN", '{"amide": [0]}', "amine"),
        ("CCO", '{"other": [0]}', "alcohol"),
        ("CCNC", '{"amide": [0], "other": [0]}', "secondary amine"),
    ):
        mol = Chem.MolFromSmiles(smiles)
        mol.SetProp("rxn_annotations", annotations)
        mol.SetProp("label", label)
        writer.write(mol)
    writer.close()


def test_shard_index_filters_reactions_scores_exactly_and_rehydrates(tmp_path):
    shard = tmp_path / "molport_processed.sdf"
    _write_shard(shard)
    reactions = tmp_path / "reactions.json"
    reactions.write_text("{}")

    build_shard_index(shard, reactions)
    index = MolportShardIndex.open(shard, reactions)
    fp_generator = get_fingerprint_generator()
    assert index.fingerprint_counts.tolist() == [
        fp_generator.GetFingerprint(Chem.MolFromSmiles("CCN")).GetNumOnBits(),
        fp_generator.GetFingerprint(Chem.MolFromSmiles("CCO")).GetNumOnBits(),
        fp_generator.GetFingerprint(Chem.MolFromSmiles("CCNC")).GetNumOnBits(),
    ]
    rows = index.eligible_rows(["amide"])
    assert rows.tolist() == [0, 2]
    assert index.building_block(2).props["label"] == "secondary amine"

    query = Chem.MolFromSmiles("CCN")
    query_fp = fp_generator.GetFingerprint(query)
    scores = index.score_rows([query_fp], np.array([query.GetNumHeavyAtoms()]), rows)[0]
    expected = get_batch_tversky_sims(
        [query_fp],
        [fp_generator.GetFingerprint(Chem.MolFromSmiles(str(index.smiles[row]))) for row in rows],
    )[0]
    sizes = index.heavy_atoms[rows]
    expected *= 1 - np.clip(sizes - query.GetNumHeavyAtoms(), 0, None) / sizes
    assert np.allclose(scores, expected)


def test_index_top_k_deduplicates_without_scanning_all_smiles(tmp_path):
    shard = tmp_path / "molport_processed.sdf"
    _write_shard(shard)
    reactions = tmp_path / "reactions.json"
    reactions.write_text("{}")
    build_shard_index(shard, reactions)
    index = MolportShardIndex.open(shard, reactions)

    # The highest-scoring raw rows are duplicates.  The selector must retain
    # the first row for that SMILES, then continue to the next distinct one.
    index.smiles = np.asarray(["CCN", "CCN", "CCO"])
    selected = MoleculeHEALER._top_unique_index_rows(
        index, np.asarray([0, 1, 2], dtype=np.uint32), np.asarray([0.9, 0.9, 0.8]), 2
    )
    assert selected == [(0.9, 0, "CCN"), (0.8, 2, "CCO")]


def test_shard_index_ignores_mtime_drift_and_rejects_missing_manifest(tmp_path):
    shard = tmp_path / "molport_processed.sdf"
    _write_shard(shard)
    reactions = tmp_path / "reactions.json"
    reactions.write_text("{}")
    build_shard_index(shard, reactions)

    os.utime(shard, ns=(shard.stat().st_atime_ns, shard.stat().st_mtime_ns + 1))
    assert MolportShardIndex.open(shard, reactions)

    assert (index_dir_for(shard) / "manifest.json").exists()
