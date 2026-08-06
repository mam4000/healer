"""
    Centralized repository for building blocks with lazy loading and caching.
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

from tqdm import tqdm
from rdkit.Chem import SDMolSupplier

from healer.domain.building_block import BuildingBlock
from healer.domain.reaction_template import ReactionTemplate21
from healer.utils.fingerprints import get_fingerprint_generator

logger = logging.getLogger(__name__)


_HEALER_PKG = Path(__file__).parent.parent
_DATA_DIR = _HEALER_PKG / "data"
_BB_DIR = Path(os.getenv("HEALER_DATA_DIR", str(_DATA_DIR / "buildingblocks")))
MOLPORT_FULL_SOURCE = "molport_full"
_MOLPORT_SUBDIR = "Molport_Full_Database"

# Named short-keys → subdirectory patterns, resolved against _BB_DIR.
# Keeping this here so the CLI (which calls resolve_bb_path directly) also
# honours HEALER_DATA_DIR.
def _build_bb_paths() -> Dict[str, str]:
    return {
        "US_stock":     str(_BB_DIR / "Enamine_Rush-Delivery_Building_Blocks-US" / "*_processed.sdf"),
        "EU_stock":     str(_BB_DIR / "Enamine_Rush-Delivery_Building_Blocks-EU" / "*_processed.sdf"),
        "Global_stock": str(_BB_DIR / "Enamine_Building_Blocks_Stock"            / "*_processed.sdf"),
        "test":         str(_BB_DIR / "test_100_bb_processed.sdf"),
    }


def resolve_bb_path(bb_source: str) -> str:
    """
        Resolve a building block source name or pattern to an actual file path.
        
        Args:
            bb_source: One of "US_stock", "EU_stock", "Global_stock", "test", 
                    or a direct file path (optionally with glob patterns).
        
        Returns:
            Resolved absolute file path.
        
        Raises:
            FileNotFoundError: If no file matches the pattern.
    """
    if bb_source == MOLPORT_FULL_SOURCE:
        shard_dir = _BB_DIR / _MOLPORT_SUBDIR
        if not any(shard_dir.glob("*_processed.sdf")):
            raise FileNotFoundError(f"No processed Molport shards found in {shard_dir}")
        return MOLPORT_FULL_SOURCE

    pattern = _build_bb_paths().get(bb_source, bb_source)
    p = Path(pattern)

    if any(ch in pattern for ch in ("*", "?", "[")):
        search_dir = p.parent if p.parent != Path() else Path.cwd()
        matches = list(search_dir.glob(p.name))
        if not matches:
            raise FileNotFoundError(f"No file matches {pattern!r}")
        matches.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        chosen = matches[0]
    else:
        chosen = p
        if not chosen.exists():
            raise FileNotFoundError(f"Building block file not found: {chosen}")

    return str(chosen)


@dataclass
class BBRepository:
    """
        Centralized repository for building blocks with lazy loading and caching.
        
        Attributes:
            source_path: Resolved path to the SDF file containing building blocks.
        
        Example:
            >>> repo = BBRepository.from_source("US_stock")
            >>> repo.load(reactions=my_reactions, show_progress=True)
            >>> bbs = repo.get_bbs_for_reactions(my_reactions)
    """

    source_path: str
    _supplier: SDMolSupplier = field(init=False, repr=False, default=None)
    _all_bbs: List[BuildingBlock] = field(default_factory=list, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)
    
    # Index mapping reaction names to sets of compatible BB indices
    _reaction_bb_indices: Dict[str, Set[int]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._supplier = SDMolSupplier(self.source_path, sanitize=True)

    @classmethod
    def from_source(cls, bb_source: str) -> "BBRepository":
        """
            Factory method to create a BBRepository from a source name or path.
            
            Args:
                bb_source: One of "US_stock", "EU_stock", "Global_stock", "test",
                        or a direct file path.
            
            Returns:
                A new BBRepository instance.
        """
        resolved_path = resolve_bb_path(bb_source)
        return cls(source_path=resolved_path)

    @property
    def total_count(self) -> int:
        """Total number of BBs in source file (before filtering)."""
        return len(self._supplier)

    @property
    def loaded_count(self) -> int:
        """Number of BBs currently loaded in memory."""
        return len(self._all_bbs)

    @property
    def is_loaded(self) -> bool:
        """Whether BBs have been loaded from source."""
        return self._loaded

    def load(self, show_progress: bool = True) -> "BBRepository":
        """
            Load ALL building blocks from the source file.
            
            All BBs are loaded regardless of reaction compatibility. The reaction
            index is built from the rxn_annotations property of each BB, allowing
            efficient filtering later via get_bbs_for_reactions().
            
            Args:
                show_progress: Whether to show a progress bar during loading.
            
            Returns:
                self (for method chaining).
        """
        if self._loaded:
            logger.debug("BBRepository already loaded, skipping reload")
            return self

        self._all_bbs = []
        self._reaction_bb_indices = {}

        fp_gen = get_fingerprint_generator()
        for mol in tqdm(
            self._supplier,
            desc="Loading building blocks",
            total=len(self._supplier),
            disable=not show_progress,
        ):
            if mol is None:
                continue
                
            bb = BuildingBlock(mol)
            bb.fingerprint = fp_gen.GetFingerprint(bb.mol)
            bb_rxn_annotations = bb.get_parsed_prop("rxn_annotations")
            
            if not isinstance(bb_rxn_annotations, dict):
                bb_rxn_annotations = {}

            # Store the BB
            bb_idx = len(self._all_bbs)
            self._all_bbs.append(bb)

            # Index by all reactions this BB is compatible with
            for rxn_name in bb_rxn_annotations.keys():
                if rxn_name not in self._reaction_bb_indices:
                    self._reaction_bb_indices[rxn_name] = set()
                self._reaction_bb_indices[rxn_name].add(bb_idx)

        self._loaded = True
        logger.info(
            "Loaded %d building blocks indexed for %d reaction types",
            len(self._all_bbs),
            len(self._reaction_bb_indices),
        )
        return self

    def get_bbs_for_reactions(
        self, reactions: List[ReactionTemplate21]
    ) -> List[BuildingBlock]:
        """
            Get BBs compatible with ANY of the given reactions.
            
            Args:
                reactions: List of reactions to filter by.
            
            Returns:
                List of BuildingBlock objects (references, not copies).
        """
        if not self._loaded:
            raise RuntimeError("BBRepository not loaded. Call load() first.")

        indices: Set[int] = set()
        for rxn in reactions:
            indices |= self._reaction_bb_indices.get(rxn.name, set())

        return [self._all_bbs[i] for i in sorted(indices)]

    def iter_bbs_for_reactions(
        self, reactions: List[ReactionTemplate21]
    ) -> Iterator[BuildingBlock]:
        """
            Memory-efficient iterator over reaction-compatible BBs.
            
            Args:
                reactions: List of reactions to filter by.
            
            Yields:
                BuildingBlock objects compatible with any of the given reactions.
        """
        if not self._loaded:
            raise RuntimeError("BBRepository not loaded. Call load() first.")

        indices: Set[int] = set()
        for rxn in reactions:
            indices |= self._reaction_bb_indices.get(rxn.name, set())

        for i in sorted(indices):
            yield self._all_bbs[i]

    def get_bb_by_index(self, idx: int) -> BuildingBlock:
        """
            Get a BB by its index in the loaded list.
            
            Args:
                idx: Index of the BB.
            
            Returns:
                The BuildingBlock at the given index.
        """
        if not self._loaded:
            raise RuntimeError("BBRepository not loaded. Call load() first.")
        return self._all_bbs[idx]

    def get_all_bbs(self) -> List[BuildingBlock]:
        """
            Get all loaded BBs.
            
            Returns:
                List of all loaded BuildingBlock objects.
        """
        if not self._loaded:
            raise RuntimeError("BBRepository not loaded. Call load() first.")
        return self._all_bbs

    def __iter__(self) -> Iterator[BuildingBlock]:
        """Iterate over all loaded BBs."""
        if not self._loaded:
            raise RuntimeError("BBRepository not loaded. Call load() first.")
        return iter(self._all_bbs)

    def __len__(self) -> int:
        """Number of loaded BBs."""
        return len(self._all_bbs)

    def __contains__(self, bb: BuildingBlock) -> bool:
        """Check if a BB is in the repository."""
        return bb in self._all_bbs

    def __getstate__(self) -> Dict[str, Any]:
        """Exclude unpicklable SDMolSupplier."""
        state = self.__dict__.copy()
        state["_supplier"] = None
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        """Restore SDMolSupplier on unpickle."""
        self.__dict__.update(state)
        self._supplier = SDMolSupplier(self.source_path, sanitize=True)


@dataclass
class ShardedBBRepository:
    """A read-through repository for a catalog stored as processed SDF shards.

    Unlike :class:`BBRepository`, this class never indexes or retains the whole
    catalog.  Each call opens one SDF shard at a time and yields compatible
    building blocks.  Consumers must keep their own bounded result set.
    """

    source_dir: str

    @property
    def is_loaded(self) -> bool:
        # There is deliberately no eager load for a sharded catalog.
        return True

    @property
    def total_count(self) -> int:
        return 0

    @property
    def loaded_count(self) -> int:
        return 0

    def __len__(self) -> int:
        return 0

    def load(self, show_progress: bool = True) -> "ShardedBBRepository":
        return self

    def shard_paths(self) -> List[Path]:
        return sorted(Path(self.source_dir).glob("*_processed.sdf"))

    def iter_bbs_for_reactions(
        self, reactions: List[ReactionTemplate21], shard_name: Optional[str] = None
    ) -> Iterator[BuildingBlock]:
        """Yield compatible blocks from all shards, or one named shard.

        ``shard_name`` is deliberately a basename rather than an arbitrary
        path: Cloud Tasks payloads are untrusted input and must not choose a
        file outside the mounted Molport catalog.
        """
        reaction_names = {reaction.name for reaction in reactions}
        shard_paths = self.shard_paths()
        if shard_name is not None:
            shard_paths = [path for path in shard_paths if path.name == shard_name]
            if not shard_paths:
                raise ValueError(f"Unknown Molport shard: {shard_name}")
        for shard_number, shard_path in enumerate(shard_paths, start=1):
            # Use WARNING intentionally: the Cloud Run service currently keeps
            # application warnings without enabling verbose module logging.
            logger.warning(
                "Molport shard %d/%d started: %s",
                shard_number,
                len(shard_paths),
                shard_path.name,
            )
            scanned = 0
            compatible = 0
            completed = False
            supplier = SDMolSupplier(str(shard_path), sanitize=True)
            try:
                for mol in supplier:
                    if mol is None:
                        continue
                    scanned += 1
                    bb = BuildingBlock(mol)
                    annotations = bb.get_parsed_prop("rxn_annotations")
                    if isinstance(annotations, dict) and reaction_names.intersection(annotations):
                        compatible += 1
                        yield bb
                completed = True
            finally:
                state = "completed" if completed else "stopped"
                logger.warning(
                    "Molport shard %d/%d %s: %s (scanned=%d, compatible=%d)",
                    shard_number,
                    len(shard_paths),
                    state,
                    shard_path.name,
                    scanned,
                    compatible,
                )

    def get_bbs_for_reactions(
        self, reactions: List[ReactionTemplate21]
    ) -> List[BuildingBlock]:
        raise RuntimeError(
            "Molport is a streaming source. Use iter_bbs_for_reactions() and retain a bounded result set."
        )


##### Module-Level Cache for Session-Wide Sharing #####

_REPOSITORY_CACHE: Dict[str, Any] = {}


def get_repository(bb_source: str) -> Any:
    """
        Get or create a BBRepository for the given source.
        
        This enables automatic sharing of BBRepository instances across
        multiple HEALER instances using the same BB source.
        
        Args:
            bb_source: One of "US_stock", "EU_stock", "Global_stock", "test",
                    or a direct file path.
        
        Returns:
            A BBRepository instance (possibly cached).
    """
    resolved_path = resolve_bb_path(bb_source)

    if resolved_path == MOLPORT_FULL_SOURCE:
        source_dir = str(_BB_DIR / _MOLPORT_SUBDIR)
        if resolved_path not in _REPOSITORY_CACHE:
            _REPOSITORY_CACHE[resolved_path] = ShardedBBRepository(source_dir=source_dir)
        return _REPOSITORY_CACHE[resolved_path]
    
    if resolved_path not in _REPOSITORY_CACHE:
        _REPOSITORY_CACHE[resolved_path] = BBRepository(source_path=resolved_path)
    
    return _REPOSITORY_CACHE[resolved_path]


def clear_repository_cache() -> None:
    """
        Clear all cached repositories.
        
        Use this between batches or when switching to different BB sources
        to free memory.
    """
    _REPOSITORY_CACHE.clear()
