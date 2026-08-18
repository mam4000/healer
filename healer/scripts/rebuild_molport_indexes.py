"""Rebuild persistent search indexes for every processed Molport SDF shard."""
from __future__ import annotations

import argparse
from pathlib import Path

from healer.domain.molport_index import MolportShardIndex, build_shard_index


def _catalog_dir(path: Path) -> Path:
    """Accept either the Molport directory itself or its buildingblocks parent."""
    return path if path.name == "Molport_Full_Database" else path / "Molport_Full_Database"


def rebuild_indexes(catalog_path: str | Path, reactions_file: str | Path) -> int:
    """Rebuild and verify every processed Molport shard index.

    The source SDF files are read only.  Each corresponding ``.index`` folder
    is replaced by ``build_shard_index``, whose manifest is published last.
    """
    catalog = _catalog_dir(Path(catalog_path).expanduser()).resolve()
    if not catalog.is_dir():
        raise FileNotFoundError(f"Molport catalog directory not found: {catalog}")
    shards = sorted(catalog.glob("*_processed.sdf"))
    if not shards:
        raise FileNotFoundError(f"No *_processed.sdf shards found in {catalog}")

    reactions = Path(reactions_file).expanduser().resolve()
    for number, shard in enumerate(shards, start=1):
        print(f"[{number}/{len(shards)}] rebuilding {shard.name}")
        build_shard_index(shard, reactions)
        index = MolportShardIndex.open(shard, reactions)
        print(f"  verified v{index.manifest['index_version']} ({index.manifest['record_count']} records)")
    return len(shards)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "catalog_path",
        help="Path to buildingblocks/ or buildingblocks/Molport_Full_Database/.",
    )
    parser.add_argument(
        "--reactions-file",
        default=Path(__file__).parents[1] / "data" / "reactions" / "reactions.json",
        help="Reaction catalog used to validate the rebuilt indexes.",
    )
    args = parser.parse_args()
    rebuild_indexes(args.catalog_path, args.reactions_file)


if __name__ == "__main__":
    main()
