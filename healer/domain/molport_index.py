"""Persistent, read-only search indexes for processed Molport SDF shards.

The index deliberately contains only data needed during candidate selection.
Molecules are reconstructed only for the final top-k records, avoiding SDF
parsing and fingerprint generation in request workers.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import SDMolSupplier

from healer.domain.building_block import BuildingBlock
from healer.utils.fingerprints import get_fingerprint_generator


INDEX_VERSION = 2
FINGERPRINT_SPEC = {"type": "morgan", "radius": 3, "fp_size": 2048, "include_chirality": True}
_BYTE_POPCOUNT = np.asarray([value.bit_count() for value in range(256)], dtype=np.uint8)


def index_dir_for(shard_path: Path) -> Path:
    return shard_path.with_name(shard_path.name + ".index")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reaction_catalog_checksum(reactions_file: Path) -> str:
    return _sha256(reactions_file)


def _json_props(mol: Chem.Mol) -> str:
    props = {
        name: BuildingBlock(mol)._parse_value(value)
        for name, value in mol.GetPropsAsDict().items()
    }
    return json.dumps(props, sort_keys=True, default=str, separators=(",", ":"))


def build_shard_index(shard_path: str | Path, reactions_file: str | Path) -> Path:
    """Build a shard index and publish its manifest last.

    A missing manifest always means the artifact is incomplete and callers must
    stream the SDF.  Array files are independently mmap-able ``.npy`` files.
    """
    shard = Path(shard_path)
    reactions = Path(reactions_file)
    destination = index_dir_for(shard)
    # Cloud Storage FUSE does not reliably support directory renames under
    # concurrent load.  The manifest is therefore the sole readiness marker:
    # write all arrays in place, then publish it atomically as the final step.
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)

    fp_generator = get_fingerprint_generator()
    smiles: list[str] = []
    props: list[str] = []
    heavy_atoms: list[int] = []
    fingerprints: list[np.ndarray] = []
    reaction_rows: dict[str, list[int]] = {}

    for mol in SDMolSupplier(str(shard), sanitize=True):
        if mol is None:
            continue
        annotation_raw = mol.GetProp("rxn_annotations") if mol.HasProp("rxn_annotations") else "{}"
        try:
            annotations = json.loads(annotation_raw)
        except json.JSONDecodeError:
            annotations = {}
        if not isinstance(annotations, dict) or not annotations:
            continue
        row = len(smiles)
        smiles.append(Chem.MolToSmiles(mol))
        props.append(_json_props(mol))
        heavy_atoms.append(mol.GetNumHeavyAtoms())
        fingerprints.append(np.frombuffer(DataStructs.BitVectToBinaryText(fp_generator.GetFingerprint(mol)), dtype=np.uint8).copy())
        for name in annotations:
            reaction_rows.setdefault(name, []).append(row)

    width = FINGERPRINT_SPEC["fp_size"] // 8
    np.save(destination / "smiles.npy", np.asarray(smiles, dtype=str), allow_pickle=False)
    np.save(destination / "props.npy", np.asarray(props, dtype=str), allow_pickle=False)
    np.save(destination / "heavy_atoms.npy", np.asarray(heavy_atoms, dtype=np.uint16), allow_pickle=False)
    fp_array = np.vstack(fingerprints).astype(np.uint8, copy=False) if fingerprints else np.empty((0, width), dtype=np.uint8)
    np.save(destination / "fingerprints.npy", fp_array, allow_pickle=False)
    # This value is used in every Tversky denominator.  Keeping it alongside
    # the packed fingerprints avoids a second complete popcount pass per
    # request.
    fingerprint_counts = _BYTE_POPCOUNT[fp_array].sum(axis=1, dtype=np.uint16)
    np.save(destination / "fingerprint_counts.npy", fingerprint_counts, allow_pickle=False)

    reaction_names = sorted(reaction_rows)
    offsets = [0]
    row_parts: list[np.ndarray] = []
    for name in reaction_names:
        rows = np.asarray(reaction_rows[name], dtype=np.uint32)
        row_parts.append(rows)
        offsets.append(offsets[-1] + len(rows))
    np.save(destination / "reaction_offsets.npy", np.asarray(offsets, dtype=np.uint64), allow_pickle=False)
    np.save(destination / "reaction_rows.npy", np.concatenate(row_parts) if row_parts else np.empty(0, dtype=np.uint32), allow_pickle=False)

    source_stat = shard.stat()
    manifest = {
        "index_version": INDEX_VERSION,
        "source_name": shard.name,
        "source_sha256": _sha256(shard),
        "source_size": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "reaction_catalog_sha256": _reaction_catalog_checksum(reactions),
        "fingerprint": FINGERPRINT_SPEC,
        "record_count": len(smiles),
        "reaction_names": reaction_names,
        "created_at": time.time(),
    }
    # Publishing this file is the readiness barrier.  A temporary file avoids
    # ever exposing a partially written manifest on FUSE/object-store mounts.
    temporary_manifest = destination / "manifest.json.tmp"
    temporary_manifest.write_text(json.dumps(manifest, sort_keys=True))
    os.replace(destination / "manifest.json.tmp", destination / "manifest.json")
    return destination


@dataclass
class MolportShardIndex:
    shard_path: Path
    root: Path
    manifest: dict[str, Any]
    smiles: np.ndarray
    props: np.ndarray
    heavy_atoms: np.ndarray
    fingerprints: np.ndarray
    fingerprint_counts: np.ndarray
    reaction_offsets: np.ndarray
    reaction_rows: np.ndarray

    @classmethod
    def open(cls, shard_path: str | Path, reactions_file: str | Path) -> MolportShardIndex:
        shard = Path(shard_path)
        root = index_dir_for(shard)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("index manifest is missing")
        manifest = json.loads(manifest_path.read_text())
        stat = shard.stat()
        valid = (
            manifest.get("index_version") == INDEX_VERSION
            and manifest.get("fingerprint") == FINGERPRINT_SPEC
            and manifest.get("reaction_catalog_sha256") == _reaction_catalog_checksum(Path(reactions_file))
            and manifest.get("source_name") == shard.name
            and manifest.get("source_size") == stat.st_size
        )
        # GCS-FUSE can expose an mtime that differs from the one observed by
        # the indexing job for the same immutable object, so it is not a
        # reliable serving-time freshness signal.  Size is cheap to check; a
        # full checksum remains available for catalog-audit deployments.
        if os.getenv("HEALER_MOLPORT_VERIFY_INDEX_CHECKSUM") == "1":
            valid = valid and manifest.get("source_sha256") == _sha256(shard)
        if not valid:
            raise ValueError("index manifest is incompatible or stale")
        try:
            arrays = [np.load(root / name, mmap_mode="r", allow_pickle=False) for name in (
                "smiles.npy", "props.npy", "heavy_atoms.npy", "fingerprints.npy", "fingerprint_counts.npy",
                "reaction_offsets.npy", "reaction_rows.npy")]
        except (OSError, ValueError) as exc:
            raise ValueError("index arrays are unreadable") from exc
        count = manifest.get("record_count")
        if any(len(array) != count for array in arrays[:5]):
            raise ValueError("index arrays have inconsistent record counts")
        return cls(shard, root, manifest, *arrays)

    def eligible_rows(self, reaction_names: Iterable[str]) -> np.ndarray:
        names = self.manifest["reaction_names"]
        requested = set(reaction_names)
        parts = []
        for position, name in enumerate(names):
            if name in requested:
                parts.append(self.reaction_rows[self.reaction_offsets[position]:self.reaction_offsets[position + 1]])
        if not parts:
            return np.empty(0, dtype=np.uint32)
        return np.unique(np.concatenate(parts))

    def building_block(self, row: int) -> BuildingBlock:
        mol = Chem.MolFromSmiles(str(self.smiles[row]))
        if mol is None:
            raise ValueError("index contains an invalid SMILES")
        bb = BuildingBlock(mol)
        for name, value in json.loads(str(self.props[row])).items():
            bb.SetProp(name, value)
        return bb

    def score_row(
        self, query_fp: Any, fragment_size: float, rows: np.ndarray, chunk_size: int = 50_000
    ) -> np.ndarray:
        """Score one fragment exactly, with bounded temporary memory."""
        scores = np.empty(len(rows), dtype=np.float64)
        if not len(rows):
            return scores
        query = np.frombuffer(DataStructs.BitVectToBinaryText(query_fp), dtype=np.uint8)
        query_count = int(_BYTE_POPCOUNT[query].sum())
        for start in range(0, len(rows), chunk_size):
            stop = min(start + chunk_size, len(rows))
            chunk_rows = rows[start:stop]
            packed = self.fingerprints[chunk_rows]
            common = _BYTE_POPCOUNT[np.bitwise_and(packed, query)].sum(axis=1)
            stock_counts = self.fingerprint_counts[chunk_rows]
            denominator = 0.95 * query_count + 0.05 * stock_counts
            similarity = np.divide(
                common, denominator, out=np.zeros_like(common, dtype=np.float64), where=denominator != 0
            )
            bb_sizes = self.heavy_atoms[chunk_rows].astype(np.float64)
            weights = 1 - np.clip(bb_sizes - fragment_size, 0, None) / bb_sizes
            scores[start:stop] = weights * similarity
        return scores

    def score_rows(self, query_fps: list, fragment_sizes: np.ndarray, rows: np.ndarray) -> np.ndarray:
        """Return exact current weighted Tversky scores without RDKit candidate objects."""
        return np.vstack([
            self.score_row(query_fp, float(fragment_sizes[index]), rows)
            for index, query_fp in enumerate(query_fps)
        ]) if query_fps else np.empty((0, len(rows)), dtype=np.float64)
