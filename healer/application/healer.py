import abc
import heapq
import hashlib
import logging
import time
from pathlib import Path
from typing import List, Union, Dict, Tuple, Any, Optional, Iterator, Iterable
from itertools import chain, islice

from tqdm import tqdm
import pandas as pd
import numpy as np
from joblib import Parallel, delayed
from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors
from prop_profiler import profile_molecules
from healer.utils.fingerprints import get_fingerprint_generator

from healer.application.tree_builder import CompositionPath, RetrosynthesisTree
from healer.domain.composition import CompositionWithBBs
from healer.application.optimizers import BaseStagewiseOptimizer, BaseSequenceOptimizer
from healer.domain.building_block import BuildingBlock
from healer.domain.reaction_template import ReactionTemplate21
from healer.domain.enumeration_record import EnumerationRecord
from healer.domain.bb_repository import BBRepository, ShardedBBRepository, get_repository
import healer.utils.utils as utils


try:
    import torch
    if not torch.cuda.is_available():
        _CUDA_AVAILABLE = False
    else:
        _CUDA_AVAILABLE = True
except ImportError:
    _CUDA_AVAILABLE = False

# Configure logging
logger = logging.getLogger(__name__)

# Get the absolute path to the healer package data directory
_HEALER_PKG = Path(__file__).parent.parent
_DATA_DIR = _HEALER_PKG / 'data'
_REACTIONS_FILE = _DATA_DIR / 'reactions' / 'reactions.json'


def _chunked(iterable, size: int):
    '''Yield successive chunks from an iterable without materialising it fully.'''
    it = iter(iterable)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            break
        yield chunk


def _apply_candidate_chunk(
    chunk: list,
) -> list:
    '''
        Process a chunk of (EnumerationRecord, BuildingBlock, ReactionTemplate21)
        tuples. Top-level function so it is picklable by loky workers.
    '''
    import healer.utils.rdkit_monkey_patch  # noqa: F401 – ensure patch in worker
    results = []
    for rec, bb, rxn in chunk:
        mol0 = rec.product
        for product in rxn.run_syn(mol0, bb):
            flags = Chem.SanitizeMol(product, catchErrors=True)
            if flags != Chem.rdmolops.SanitizeFlags.SANITIZE_NONE:
                continue
            results.append(
                EnumerationRecord(
                    product=product,
                    bbs=rec.bbs + [bb],
                    reaction_names=rec.reaction_names + [rxn.name],
                    props=dict(rec.props),
                )
            )
    return results


class _BaseHEALER(abc.ABC):
    '''
        Base HEALER.
    '''
    def __init__(
        self, 
        bb_source: str, 
        reaction_tags: Union[List[str], str],
        bb_repository: Optional[BBRepository] = None,
        shuffle_bb_order: bool = False,
        verbose: int = 1,
    ) -> None:
        '''
            Initialize BaseHEALER.

            Args:
                bb_source: one of "US_stock", "EU_stock" or "Global_stock"; or path to an SDF file.
                reaction_tags: list of tags or 'all'.
                bb_repository: optional pre-loaded BBRepository for sharing across instances.
                shuffle_bb_order: whether to shuffle the order of BBs after loading.
                verbose: verbosity level.
                    - 0: WARNING
                    - 1: INFO
                    - 2: DEBUG
        '''
        # Set verbosity level (0=WARNING, 1=INFO, 2+=DEBUG)
        self.verbose: int = verbose

        # Core attributes
        self.query_mol: Chem.Mol = None
        self._compositions: List[Union[CompositionPath, CompositionWithBBs]] = []
        self.enumerated_molecules: List[EnumerationRecord] = []

        # Reaction attributes
        self.reactions: List[ReactionTemplate21] = []
        self.reaction_tags: Union[List[str], str] = []
        self._reactions: List[ReactionTemplate21] = []  # all reactions loaded from JSON

        # Load reactions first (needed to filter BBs)
        self._load_reactions(reaction_tags)

        # Use injected repository or get/create one via cache
        if bb_repository is not None:
            self._bb_repo = bb_repository
        else:
            self._bb_repo = get_repository(bb_source)
        
        # Ensure BBs are loaded (reaction-agnostic, loads all BBs)
        if not self._bb_repo.is_loaded:
            self._bb_repo.load(show_progress=verbose >= 1)

        # Optional shuffling (creates a shuffled index, not a copy)
        self._bb_shuffle_indices: Optional[np.ndarray] = None
        if shuffle_bb_order and not isinstance(self._bb_repo, ShardedBBRepository):
            self._bb_shuffle_indices = np.random.permutation(len(self._bb_repo))
        elif shuffle_bb_order:
            logger.warning("shuffle_bb_order is ignored for streaming building-block sources")

        # Fingerprint generator
        self._fp_generator = get_fingerprint_generator()

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['_fp_generator']
        return state
    
    def __setstate__(self, state):
        self.__dict__.update(state)
        self._fp_generator = get_fingerprint_generator()

    @property
    def bb_mols(self) -> List[BuildingBlock]:
        '''Get building blocks compatible with current reactions.'''
        bbs = self._bb_repo.get_bbs_for_reactions(self.reactions)
        if self._bb_shuffle_indices is not None:
            # Apply shuffle - filter indices to valid range
            valid_indices = self._bb_shuffle_indices[self._bb_shuffle_indices < len(bbs)]
            return [bbs[i] for i in valid_indices]
        return bbs

    def _load_reactions(self, reaction_tags: Union[List[str], str]) -> None:
        '''
            Load and filter reactions based on tags.

            Args:
                reaction_tags: a reaction tag or a list of tags to filter by.
                    'all' to use all reactions.
        '''
        all_rxns = utils.load_reactions_from_json(str(_REACTIONS_FILE))
        self._reactions = [r for r in all_rxns if r.is_valid()]
        all_tags = list(set(chain(*[r.tags for r in self._reactions])))
        
        if isinstance(reaction_tags, str):
            if reaction_tags.lower() == 'all':
                self.reaction_tags = all_tags
                self.reactions = self._reactions
            else:
                self.reaction_tags = [reaction_tags]
                self.reactions = [r for r in self._reactions if reaction_tags in r.tags]
        else:
            if 'all' in [tag.lower() for tag in reaction_tags]:
                self.reaction_tags = all_tags
                self.reactions = self._reactions
            else:
                self.reaction_tags = [tag for tag in reaction_tags if tag in all_tags]
                self.reactions = [r for r in self._reactions if any(tag in r.tags for tag in self.reaction_tags)]
        
        logger.debug("Loaded %d reactions for tags: %s", len(self.reactions), self.reaction_tags)

    def set_reactions(self, reaction_tags: Union[List[str], str]) -> None:
        '''
            Update the reactions used for enumeration. This also updates the 
            building blocks returned by `bb_mols` since they are filtered by 
            reaction compatibility.

            Args:
                reaction_tags: a reaction tag, list of tags, or 'all'.
        '''
        if not self._reactions:
            # Reactions haven't been loaded yet, do full load
            self._load_reactions(reaction_tags)
            return
        
        all_tags = list(set(chain(*[r.tags for r in self._reactions])))
        
        if isinstance(reaction_tags, str):
            if reaction_tags == 'all':
                self.reaction_tags = all_tags
                self.reactions = self._reactions
            else:
                self.reaction_tags = [reaction_tags]
                self.reactions = [r for r in self._reactions if reaction_tags in r.tags]
        else:
            if 'all' in reaction_tags:
                self.reaction_tags = all_tags
                self.reactions = self._reactions
            else:
                self.reaction_tags = [tag for tag in reaction_tags if tag in all_tags]
                self.reactions = [r for r in self._reactions if any(tag in r.tags for tag in self.reaction_tags)]
        
        # Reset compositions since they may depend on reactions
        self._compositions = []
        
        logger.debug("Updated to %d reactions with tags: %s", len(self.reactions), self.reaction_tags)

    def enumerate(
        self, 
        optimizer: Optional[Union[BaseStagewiseOptimizer, BaseSequenceOptimizer]] = None,
        max_evals_per_comp: Optional[int] = None,
        max_products_per_comp: Optional[int] = None,
        max_total_products: Optional[int] = None,
        n_jobs: int = 1,
    ) -> None:
        '''
            Enumerate the molecule with building blocks based on the reactions. An optimizer
            can be provided to optimize an objective function during enumeration.

            Args:
                optimizer: an optimizer object to use for enumeration.
                max_evals_per_comp: maximum number of evaluations for each composition.
                max_products_per_comp: maximum number of products per composition.
                max_total_products: maximum number of total products.
                n_jobs: number of parallel threads for synthesis. 1 = sequential (default),
                    -1 = use all CPUs.

            Raises:
                TypeError: if the optimizer is not of a supported type.
        '''
        self.enumerated_molecules = [
            EnumerationRecord(
                product=self.query_mol,
                bbs=[],
                reaction_names=[],
                props={'optimization_score': optimizer.target_fn(self.query_mol)} if optimizer else {}
            )
        ]

        if not isinstance(self.query_mol, Chem.Mol):
            raise ValueError("Query molecule must be set before enumeration. Use set_query_mol() method.")
        self._process_query_mol()
        self._process_building_blocks()

        if optimizer is None:
            self.enumerated_molecules += self._enumerate_base(
                max_evals_per_comp, max_products_per_comp, max_total_products, n_jobs
            )
        elif isinstance(optimizer, BaseStagewiseOptimizer):
            self.enumerated_molecules += self._enumerate_stagewise(
                optimizer, max_evals_per_comp, max_products_per_comp, max_total_products, n_jobs
            )
        elif isinstance(optimizer, BaseSequenceOptimizer):
            self.enumerated_molecules += self._enumerate_sequence(
                optimizer, max_evals_per_comp, max_products_per_comp, max_total_products, n_jobs
            )
        else:
            raise TypeError(f"Unsupported optimizer type: {type(optimizer)}. ")
    
    @abc.abstractmethod
    def set_query_mol(self, molecule: Union[str, Chem.Mol]) -> None:
        '''
            Set the query molecule for enumeration and set the attributes that will be used
            to process the query molecule. The purpose of this separation is to allow enumeration 
            of multiple molecules without reinitializing the HEALER instance.
        '''
        ...

    @abc.abstractmethod
    def _process_query_mol(self) -> None:
        '''
            Should update self._compositions with a list of `CompositionPath` objects.
        '''
        ...

    @abc.abstractmethod
    def _process_building_blocks(self) -> None:
        '''
            Should convert the `CompositionPath` to `CompositionWithBBs` objects 
            inside `self._compositions` by pairing each fragment with a list of 
            compatible building blocks.
        '''
        ...
    
    def _enumerate_base(
        self,
        max_evals_per_comp: Optional[int] = None,
        max_products_per_comp: Optional[int] = None,
        max_total_products: Optional[int] = None,
        n_jobs: int = 1,
    ) -> List[EnumerationRecord]:
        '''
            Exhaustive enumeration without optimization.
        '''
        results: List[EnumerationRecord] = []
        for comp_bb in tqdm(self._compositions, desc="Enumerating compositions", disable=self.verbose >= 2):
            bb_lists = comp_bb.fragment_bbs 
            stage_records = self._make_seed_records(bb_lists[0])
            eval_count = 0
            
            for bb_pool in bb_lists[1:]:
                cands = self._generate_candidates(stage_records, bb_pool)
                # Soft limit: truncate candidates if we'd exceed max_evals_per_comp
                if max_evals_per_comp:
                    remaining = max_evals_per_comp - eval_count
                    if remaining <= 0:
                        break
                    cands = islice(cands, remaining)
                # Note: eval_count becomes approximate since we don't know exact count
                # without consuming the generator. We count applied candidates instead.
                prev_count = len(stage_records)
                stage_records = self._apply_candidates(cands, n_jobs=n_jobs)
                eval_count += len(stage_records) - prev_count if prev_count else len(stage_records)

            # Soft limit: truncate products per composition
            if max_products_per_comp:
                stage_records = stage_records[:max_products_per_comp]
            
            results.extend(stage_records)
            
            # Hard limit: stop if total products reached
            if max_total_products and len(results) >= max_total_products:
                results = results[:max_total_products]
                break

        logger.debug("Enumeration completed with %d results", len(results))
        return results
    
    def _enumerate_stagewise(
        self, 
        optimizer: BaseStagewiseOptimizer, 
        max_evals_per_comp: Optional[int] = None,
        max_products_per_comp: Optional[int] = None,
        max_total_products: Optional[int] = None,
        n_jobs: int = 1,
    ) -> List[EnumerationRecord]:
        '''
            Stagewise enumeration with optimizer.filter() hook.
        '''
        results: List[EnumerationRecord] = []
        for comp_bb in tqdm(self._compositions, desc="Enumerating compositions", disable=self.verbose >= 2):
            bb_lists = comp_bb.fragment_bbs
            stage_records = self._make_seed_records(bb_lists[0])
            eval_count = 0
            
            for depth, bb_pool in enumerate(bb_lists[1:], start=1):
                cands = self._generate_candidates(stage_records, bb_pool)
                cands = optimizer.filter(cands, depth)  # filter can return generator or list
                # Soft limit: truncate candidates if we'd exceed max_evals_per_comp
                if max_evals_per_comp:
                    remaining = max_evals_per_comp - eval_count
                    if remaining <= 0:
                        break
                    cands = islice(cands, remaining)
                # Count based on results since generator length is unknown
                prev_count = len(stage_records)
                stage_records = self._apply_candidates(cands, n_jobs=n_jobs)
                eval_count += len(stage_records) - prev_count if prev_count else len(stage_records)

            # Score and collect products
            comp_products = []
            for rec in stage_records:
                rec.props.update({'optimization_score': optimizer.target_fn(rec.product)})
                comp_products.append(rec)
                # Soft limit: stop collecting from this composition
                if max_products_per_comp and len(comp_products) >= max_products_per_comp:
                    break
            
            results.extend(comp_products)
            
            # Hard limit: stop if total products reached
            if max_total_products and len(results) >= max_total_products:
                results = results[:max_total_products]
                break

        logger.debug("Stagewise enumeration completed with %d results", len(results))
        return results
    
    def _enumerate_sequence(
        self,
        optimizer: BaseSequenceOptimizer,
        max_evals_per_comp: Optional[int] = None,
        max_products_per_comp: Optional[int] = None,
        max_total_products: Optional[int] = None,
        n_jobs: int = 1,
    ) -> List[EnumerationRecord]:
        '''
            Sequence-based enumeration using optimizer.ask() and optimizer.tell().
        '''     
        results: List[EnumerationRecord] = []
        for comp_bb in self._compositions:
            eval_count = 0
            prod_count = 0
            optimizer.init_search(
                domain=comp_bb.fragment_bbs, 
                budget=max_evals_per_comp or 0
            )
            
            while True:
                bb_tuples = optimizer.ask()
                if not bb_tuples:
                    break

                bb_pools = [list(bb_tuple) for bb_tuple in zip(*bb_tuples)]
                stage_records = self._make_seed_records(bb_pools[0])  
                
                for bb_pool in bb_pools[1:]:
                    cands = self._generate_candidates_positionwise(stage_records, bb_pool)
                    # Soft limit: truncate candidates
                    if max_evals_per_comp:
                        remaining = max_evals_per_comp - eval_count
                        if remaining <= 0:
                            cands = iter([])  # empty iterator
                        else:
                            cands = islice(cands, remaining)
                    # Count based on results since generator length is unknown
                    prev_count = len(stage_records)
                    stage_records = self._apply_candidates(cands, n_jobs=n_jobs)
                    eval_count += len(stage_records) - prev_count if prev_count else len(stage_records)

                # Collect feedback and products
                feedback: List[Tuple[Tuple[BuildingBlock, ...], float]] = []
                for rec in stage_records:
                    score = optimizer.target_fn(rec.product)
                    rec.props.update({'optimization_score': score})
                    feedback.append((tuple(rec.bbs), score))
                    results.append(rec)
                    prod_count += 1
                    # Soft limit: stop collecting from this composition
                    if max_products_per_comp and prod_count >= max_products_per_comp:
                        break
                    # Hard limit: stop completely
                    if max_total_products and len(results) >= max_total_products:
                        break

                optimizer.tell(feedback)
                
                # Check all limits to break ask/tell loop
                if max_evals_per_comp and eval_count >= max_evals_per_comp:
                    break
                if max_products_per_comp and prod_count >= max_products_per_comp:
                    break
                if max_total_products and len(results) >= max_total_products:
                    break

            # Hard limit: stop enumeration entirely
            if max_total_products and len(results) >= max_total_products:
                results = results[:max_total_products]
                break

        logger.debug("Sequence enumeration completed with %d results", len(results))
        return results

    def _make_seed_records(self, bb0_pool: List[BuildingBlock]) -> List[EnumerationRecord]:
        '''
            Create initial enumeration records from the seed building blocks.
        '''
        return [
            EnumerationRecord(
                product=bb.mol, bbs=[bb], reaction_names=[], props={}
            ) for bb in bb0_pool
        ]
    
    def _generate_candidates_positionwise(
        self,
        stage_records: List[EnumerationRecord],
        bb_pool: List[BuildingBlock]
    ) -> Iterator[Tuple[EnumerationRecord, BuildingBlock, ReactionTemplate21]]:
        '''
            One-to-one mapping of enumeration records to building blocks. Pair 
            each current record with the BB at the same index for each reaction.
            Yields candidates lazily to avoid materializing large lists.
        '''
        for rxn in self.reactions:
            for rec, bb in zip(stage_records, bb_pool):
                yield (rec, bb, rxn)

    def _generate_candidates(
        self, 
        stage_records: List[EnumerationRecord], 
        bb_pool: List[BuildingBlock]
    ) -> Iterator[Tuple[EnumerationRecord, BuildingBlock, ReactionTemplate21]]:
        '''
            Build all (rec, bb, rxn) triples for the next coupling.
            Yields candidates lazily to avoid materializing large lists.
        '''
        for rxn in self.reactions:
            for rec in stage_records:
                for bb in bb_pool:
                    yield (rec, bb, rxn)
    
    def _apply_candidates(
        self,
        candidates: Iterable[Tuple[EnumerationRecord, BuildingBlock, ReactionTemplate21]],
        n_jobs: int = 1,
    ) -> List[EnumerationRecord]:
        '''
            Batch-apply all candidates via _apply_candidate.
            Accepts any iterable (list, generator, islice, etc.).
        '''
        if n_jobs == 1:
            next_stage_records: List[EnumerationRecord] = []
            for rec, bb, rxn in candidates:
                next_stage_records += self._apply_candidate(rec, bb, rxn)
            return next_stage_records

        chunk_size = 1000
        nested: List[List[EnumerationRecord]] = Parallel(
            n_jobs=n_jobs,
            backend='loky',
            pre_dispatch='2*n_jobs',
        )(
            delayed(_apply_candidate_chunk)(chunk)
            for chunk in _chunked(candidates, chunk_size)
        )
        return [rec for batch in nested for rec in batch]    
    
    def _apply_candidate(
        self,
        rec: EnumerationRecord,
        bb: BuildingBlock,
        rxn: ReactionTemplate21
    ) -> List[EnumerationRecord]:
        '''
            Apply the reaction to the record and building block, returning new records.
        '''
        results: List[EnumerationRecord] = []
        mol0 = rec.product
        for product in rxn.run_syn(mol0, bb):
            flags = Chem.SanitizeMol(product, catchErrors=True)
            if flags != Chem.rdmolops.SanitizeFlags.SANITIZE_NONE:
                continue
            new_props = dict(rec.props)
            results.append(
                EnumerationRecord(
                    product=product,
                    bbs=rec.bbs + [bb],
                    reaction_names=rec.reaction_names + [rxn.name],
                    props=new_props
                )
            )
        return results

    def get_results(
        self, 
        as_dict: bool = False,
        calc_similarity: bool = True,
        calc_properties: bool = True,
        skip_cns_mpo: bool = True,
    ) -> Union[pd.DataFrame, List[Dict[str, Any]]]:
        '''
            Get the results of enumeration as a pandas DataFrame or a list of dicts.

            Args:
                as_dict (bool): if True, return the results as a list of dicts.
                calc_similarity (bool): if True, calculate similarity to the query molecule.
                calc_properties (bool): if True, calculate additional properties.
                skip_cns_mpo (bool): if True, skip calculating CNS MPO scores when calculating properties.
        '''
        max_bb = None
        if isinstance(self, MoleculeHEALER) and not isinstance(self, FragmentHEALER):
            max_bb = 2 ** self.retro_tree_depth
        elif isinstance(self, SiteHEALER):
            max_bb = 2    
        else:
            max_bb = max(len(r.bbs) for r in self.enumerated_molecules)
        
        max_rxn = max_bb - 1

        # Build each row as a dict
        rows = []
        for r in self.enumerated_molecules:
            row = {"Product": Chem.MolToSmiles(r.product),}
            for i in range(max_bb):
                row[f"BB{i+1}"] = r.bbs[i].get_smiles() if i < len(r.bbs) else ""
            for i in range(max_rxn):
                row[f"Reaction{i+1}_name"] = (
                    r.reaction_names[i] if i < len(r.reaction_names) else ""
                )
            for i in range(max_bb):
                row[f"URL{i+1}"] = r.bbs[i].get_url() if i < len(r.bbs) else ""
            row = {**row, **r.props}  # Add any additional properties
            rows.append(row)

        df = pd.DataFrame(rows)
        cols_to_consider = [col for col in df.columns if not col.startswith(("URL", "Reaction"))]
        df = df.drop_duplicates(subset=cols_to_consider, keep="first", ignore_index=True)
        
        if calc_similarity:
            enum_fps = self._get_fingerprints(df['Product'].apply(Chem.MolFromSmiles).tolist())
            query_fp = enum_fps.pop(0)  
            tani_sims = utils.get_batch_tani_sims([query_fp], enum_fps)[0]
            tani_sims = np.concat((np.array([1.001]), tani_sims))
            df['Similarity_to_query'] = tani_sims
            df = df.sort_values(by='Similarity_to_query', ascending=False, ignore_index=True)
            df = df.round({'Similarity_to_query': 2})
        
        if calc_properties:
            profile_df = profile_molecules(
                molecules=df['Product'].tolist(),
                skip_cns_mpo=skip_cns_mpo,
                device="cuda" if _CUDA_AVAILABLE else "cpu",
                verbose=bool(self.verbose >= 1),
            )
            profile_df.rename(columns={'smiles': 'Product'}, inplace=True)
            df = df.merge(profile_df, how='left', on='Product', validate='m:1')

        df.insert(0, "ID", [f"{i:06d}" for i in df.index])
        if as_dict:
            return df.to_dict(orient="records")
        return df

    def save_results(self, path: str='results.csv', **kwargs) -> None:
        '''
            Save results to a CSV file.
            
            Args:
                path: path to save the results.
                **kwargs: additional arguments for get_results.
        '''
        df = self.get_results(**kwargs)
        df.to_csv(path, index=False)

    def _get_fingerprints(self, mols: List[Chem.Mol]) -> List[Any]:
        return list(self._fp_generator.GetFingerprints(mols))
        
    def _get_fingerprint(self, mol: Chem.Mol) -> Any:
        return self._fp_generator.GetFingerprint(mol)


class SiteHEALER(_BaseHEALER):
    '''
        Site HEALER: enumerates molecules by applying reactions to a query molecule
        at specified reactive sites.
    '''
    def __init__(
            self, 
            bb_source: str = 'US_stock',
            reaction_tags: list[str] | str = ['amide coupling', 'amide', 'C-N bond formation', 'C-N',
                                            'alkylation', 'N-arylation', 'azole', 'amination'],
            bb_repository: Optional[BBRepository] = None,
            shuffle_bb_order: bool = False,
            rules: dict[str, tuple[int, int]] = {
                'MW': (0, 500), # molecular weight
                'HBD': (0, 5), # hydrogen bond donors
                'HBA': (0, 10), # hydrogen bond acceptors
                'TPSA': (0, 200), # topological polar surface area
                'RotB': (0, 10), # rotatable bonds
                'Rings': (0, 10), # number of rings
                'ArRings': (0, 5), # number of aromatic rings
                'Chiral': (0, 5), # number of chiral centers
            },
            struct_rules: list[str]=[],
            max_bbs: int = 10,
            verbose: int=1,
    ):
        '''
            Initialize SiteHEALER.

            Args:
                bb_source: one of "US_stock", "EU_stock" or "Global_stock"; or path to an SDF file.
                reaction_tags: list of tags or 'all'.
                bb_repository: optional pre-loaded BBRepository for sharing across instances.
                shuffle_bb_order: whether to shuffle the order of building blocks.
                rules: dictionary of rules for filtering molecules.
                struct_rules: list of structural rules for filtering molecules.
                verbose: verbosity level, 0 for errors, 1 for warnings, 2 for info.
        '''
        super().__init__(bb_source, reaction_tags, bb_repository, shuffle_bb_order, verbose)
        self.rules = rules
        self.struct_rules = struct_rules
        self.max_bbs = max_bbs
    
    def set_rules(self, **kwargs):
        '''
            Set the rules for the building blocks.

            Args:
                kwargs: dictionary containing the rules.
                    MW: tuple (min, max) -- molecular weight
                    HBD: tuple (min, max) -- hydrogen bond donors
                    HBA: tuple (min, max) -- hydrogen bond acceptors
                    TPSA: tuple (min, max) -- topological polar surface area
                    RotB: tuple (min, max) -- rotatable bonds
                    Rings: tuple (min, max) -- number of rings
                    ArRings: tuple (min, max) -- number of aromatic rings
                    Chiral: tuple (min, max) -- number of chiral centers
        '''
        for key, value in kwargs.items():
            if key in self.rules:
                self.rules[key] = value
            else:
                raise ValueError(f'Invalid rule: {key}')
    
    def set_query_mol(
        self, 
        query_mol: Union[str, Chem.Mol], 
        reactive_sites: Optional[List[int]] = None
    ) -> None:
        '''
            Set the query molecule for enumeration and reactive sites.

            Args:
                query_mol: a SMILES string or an RDKit Mol object.
                reactive_sites: list of indices of reactive sites in the molecule.
        '''
        if isinstance(query_mol, str):
            self.query_mol = Chem.MolFromSmiles(query_mol)
        else:
            self.query_mol = query_mol
        flag = Chem.SanitizeMol(self.query_mol, catchErrors=True)
        assert flag == Chem.rdmolops.SanitizeFlags.SANITIZE_NONE, f"SanitizeMol failed: {flag}"

        self.reactive_sites = reactive_sites if reactive_sites else []

        self._compositions = []     # reset compositions

    def _process_query_mol(self, protect_neighbors: bool=False) -> None:
        if not isinstance(self.query_mol, Chem.Mol):
            raise ValueError('Query molecule must be an RDKit Mol object.')
        
        query_mol = self.query_mol
        if self.reactive_sites:
            dont_protect = set()
            for atom in query_mol.GetAtoms():
                if atom.GetIdx() in self.reactive_sites:
                    dont_protect.add(atom.GetIdx())
                    if not protect_neighbors:
                        for neighbor in atom.GetNeighbors():
                            dont_protect.add(neighbor.GetIdx())
            for atom in query_mol.GetAtoms():
                if atom.GetIdx() not in dont_protect:
                    atom.SetProp('_protected', '1')
        else:
            logger.warning('No reactive sites provided! All atoms will be considered reactive.')

        comp = CompositionPath(fragments=(query_mol,))
        self._compositions.append(comp)

    def _process_building_blocks(self) -> None:
        '''Filter building blocks based on the rules and structure-based rules.'''
        if isinstance(self._bb_repo, ShardedBBRepository):
            if self.max_bbs <= 0:
                raise ValueError("Streaming building-block sources require max_bbs to be positive")
            # Keep a deterministic, bounded sample across all shards.  A site
            # search has no similarity score to rank by, so a stable hash avoids
            # favouring the first file in catalog order.
            selected: List[Tuple[str, BuildingBlock]] = []
            for bb in self._bb_repo.iter_bbs_for_reactions(self.reactions):
                try:
                    if not (self._check_rules(bb) and self._check_struct_rules(bb)):
                        continue
                    rank = hashlib.sha256(bb.get_smiles().encode()).hexdigest()
                    if len(selected) < self.max_bbs:
                        selected.append((rank, bb))
                    else:
                        worst_idx = max(range(len(selected)), key=lambda i: selected[i][0])
                        if rank < selected[worst_idx][0]:
                            selected[worst_idx] = (rank, bb)
                finally:
                    bb.evict()
            filtered_bbs = [bb for _, bb in sorted(selected)]
            self._compositions = [
                CompositionWithBBs(
                    comp=comp,
                    fragment_bbs=([BuildingBlock(comp.fragments[0])], filtered_bbs),
                ) for comp in self._compositions
            ]
            return

        bb_mols = self.bb_mols
        filtered_bbs = []
        for bb in bb_mols:
            if self._check_rules(bb) and self._check_struct_rules(bb):
                filtered_bbs.append(bb)
            bb.evict()    # free mol memory after checking rules
        self._compositions = [
            CompositionWithBBs(
                comp=comp,
                fragment_bbs=([BuildingBlock(comp.fragments[0])], filtered_bbs)
            ) for comp in self._compositions
        ]

    def _check_struct_rules(self, building_block: Union[Chem.Mol, BuildingBlock]) -> bool:
        '''
            Check if the building block satisfies the structure-based rules.
        '''
        if not self.struct_rules:
            return True
        for rule in self.struct_rules:
            if not building_block.HasSubstructMatch(Chem.MolFromSmarts(rule)):
                return False
        return True

    def _check_rules(self, building_block: Union[Chem.Mol, BuildingBlock]) -> bool:
        '''
            Check if the building block satisfies the rules.
        '''
        mol = building_block.mol if isinstance(building_block, BuildingBlock) else building_block

        for key, value in self.rules.items():
            if key == 'MW':
                if not value[0] <= Descriptors.MolWt(mol) <= value[1]:
                    return False
            elif key == 'HBD':
                if not value[0] <= Descriptors.NumHDonors(mol) <= value[1]:
                    return False
            elif key == 'HBA':
                if not value[0] <= Descriptors.NumHAcceptors(mol) <= value[1]:
                    return False
            elif key == 'TPSA':
                if not value[0] <= Descriptors.TPSA(mol) <= value[1]:
                    return False
            elif key == 'RotB':
                if not value[0] <= Descriptors.NumRotatableBonds(mol) <= value[1]:
                    return False
            elif key == 'Rings':
                if not value[0] <= Descriptors.RingCount(mol) <= value[1]:
                    return False
            elif key == 'ArRings':
                if not value[0] <= Descriptors.NumAromaticRings(mol) <= value[1]:
                    return False
            elif key == 'Chiral':
                if not value[0] <= rdMolDescriptors.CalcNumAtomStereoCenters(mol) <= value[1]:
                    return False
            else:
                raise ValueError(f'Invalid rule: {key}')
            
        return True


class MoleculeHEALER(_BaseHEALER):
    '''
        Molecule HEALER: enumerates molecules by splitting them into fragments and
        applying reactions to each fragment. It can also use custom split sites to
        generate compositions from the query molecule.
    '''
    def __init__(
        self, 
        bb_source: str = 'US_stock', 
        reaction_tags: list[str] = ['amide coupling', 'amide', 'C-N bond formation', 'C-N',
                                    'alkylation', 'N-arylation', 'azole', 'amination'],
        bb_repository: Optional[BBRepository] = None,
        shuffle_bb_order: bool = False,
        sim_threshold: float = 0.5,
        max_bbs_per_frag: int = -1,
        verbose: int = 1,
    ):
        '''
            Initialize MoleculeHEALER.

            Args:
                bb_source: one of "US_stock", "EU_stock" or "Global_stock"; or path to an SDF file.
                reaction_tags: list of tags or 'all'.
                bb_repository: optional pre-loaded BBRepository for sharing across instances.
                shuffle_bb_order: whether to shuffle the order of building blocks.
                sim_threshold: similarity threshold for filtering building blocks.
                max_bbs_per_frag: maximum number of building blocks per fragment.
                    If <= 0, all building blocks will be considered. Otherwise, the similarity
                    threshold will be adjusted to the number of building blocks.
                verbose: verbosity level, 0 for errors, 1 for warnings, 2 for info.
        '''
        super().__init__(bb_source, reaction_tags, bb_repository, shuffle_bb_order, verbose)
        self.sim_threshold = sim_threshold
        self.max_bbs_per_frag = max_bbs_per_frag

    def set_query_mol(
        self, 
        query_mol: Union[str, Chem.Mol], 
        n_compositions: int=10,
        randomize_compositions: bool=False,
        random_seed: int=-1,
        custom_split_sites: Optional[List[List[Tuple[int, int]]]] = None,
        retro_tree_depth: int = 1,
        min_frag_size: int = 3,
    ) -> None:
        '''
            Set the query molecule for enumeration and custom split sites.

            Args:
                query_mol: a SMILES string or an RDKit Mol object.
                n_compositions: number of compositions to consider for enumeration. Higher values
                    will increase the diversity.
                randomize_compositions: if True, randomize the order of compositions, 
                    otherwise sorted by the number of fragments. Randomization may increase
                    the diversity but also lead to more reaction steps.
                random_seed: seed for randomization, -1 for no specified seed.
                custom_split_sites: Custom split sites for the molecule. If provided, 
                    the molecule will be split into fragments based on these sites.
                    Each site is a tuple of atom indices (start, end) to split the molecule.
                    A molecule can have multiple split sites to generate multiple fragments.
                    For example, if `custom_split_sites = [[(0, 2), (3, 5)], [(1, 4)]]`, then 
                    there will be two separate compositions generated from the molecule.
                retro_tree_depth: depth of retrosynthesis tree to generate compositions.
                min_frag_size: minimum number of heavy atoms in a fragment to consider it valid.
        '''
        if isinstance(query_mol, str):
            self.query_mol = Chem.MolFromSmiles(query_mol, sanitize=False)
        else:
            self.query_mol = query_mol
        flag = Chem.SanitizeMol(self.query_mol, catchErrors=True)
        assert flag == Chem.rdmolops.SanitizeFlags.SANITIZE_NONE, f"SanitizeMol failed: {flag}"

        if len(Chem.GetMolFrags(self.query_mol, sanitizeFrags=False)) > 1:
            raise ValueError('Query molecule must be a single connected component. '
                             'Use FragmentHEALER for multi-component molecules.')

        self.n_compositions = n_compositions
        self.randomize_compositions = randomize_compositions
        self.random_seed = random_seed
        self.custom_split_sites = custom_split_sites if custom_split_sites else []
        self.retro_tree_depth = retro_tree_depth
        self.min_frag_size = min_frag_size
        
        self._compositions = []     # reset compositions

    def _process_query_mol(self) -> None:
        '''
            Process the query molecule to generate compositions based on the 
            custom split sites if provided, or create retrosynthesis tree 
            with all possible compositions.
        '''
        if not isinstance(self.query_mol, Chem.Mol):
            raise ValueError('Query molecule must be an RDKit Mol object. Set it using set_query_mol() method.')
        
        if self.custom_split_sites:
            logger.debug("Using custom split sites: %s", self.custom_split_sites)
            for split_sites in self.custom_split_sites:
                fragments = self._split_molecule(split_sites)
                if len(fragments) < 2:
                    logger.warning(f'Custom split sites {split_sites} did not produce multiple fragments. '
                                   'Skipping this composition.')
                else:
                    comp = CompositionPath(fragments=fragments)
                    self._compositions.append(comp)
        else:
            logger.debug("No custom split sites. Generating retrosynthesis tree.")
            retro_tree = RetrosynthesisTree(
                self.query_mol, 
                self.reactions, 
                max_depth=self.retro_tree_depth, 
                min_heavy_atoms=self.min_frag_size
            )
            retro_tree.build()
            self._compositions = retro_tree.get_composition_paths(self.randomize_compositions, self.random_seed)

        # Log compositions at debug level
        logger.debug("Generated %d composition(s):\n%s", len(self._compositions), self._composition_prints())

        if len(self._compositions) > self.n_compositions:
            self._compositions = self._compositions[:self.n_compositions]
        
    def _split_molecule(self, split_sites: List[Tuple[int, int]]) -> List[Chem.Mol]:
        '''
            Split the molecule into fragments based on the provided split sites.
            Returns a list of fragments as RDKit Mol objects.
        '''
        with Chem.RWMol(self.query_mol) as rw_mol:
            for start, end in split_sites:
                rw_mol.RemoveBond(start, end)
        mol = rw_mol.GetMol()
        return Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=True)

    def _composition_prints(self) -> str:
        '''
            Return a loggable string representation of the compositions.
        '''
        if not self._compositions:
            return 'No compositions found.'
        if isinstance(self._compositions[0], CompositionPath):
            return '\n'.join(
                f'Composition {i+1} fragments: {[Chem.MolToSmiles(frag) for frag in comp.fragments]}'
                for i, comp in enumerate(self._compositions)
            )
        
        elif isinstance(self._compositions[0], CompositionWithBBs):
            return '\n'.join(
                f'Composition {i+1} fragments: {[Chem.MolToSmiles(frag) for frag in comp.comp.fragments]}'
                for i, comp in enumerate(self._compositions)
            )
        
        else:
            raise TypeError(
                f'Invalid type for compositions. Expected CompositionPath or CompositionWithBBs, '
                f'got {type(self._compositions[0])}'
            )

    def _process_building_blocks(self, bb_chunk_size: int=10000) -> None:
        '''
            Process building blocks to filter them based on the similarity 
            to the query molecule and the number of building blocks per composition 
            if given.
        '''
        if isinstance(self._bb_repo, ShardedBBRepository):
            self._process_sharded_building_blocks(bb_chunk_size)
            return

        bb_mols = self.bb_mols
        bb_sizes = np.array([bb.num_heavy_atoms for bb in bb_mols])
        bb_fps = [bb.fingerprint for bb in bb_mols]
        n_bbs = len(bb_mols)

        frag_lists = [path.fragments for path in self._compositions]
        offsets = np.concatenate(([0], np.cumsum([len(frag_list) for frag_list in frag_lists])))
        frags_flatten = [frag for frag_list in frag_lists for frag in frag_list]
        frag_sizes = np.array([frag.GetNumHeavyAtoms() for frag in frags_flatten])[:, None]
        frag_fps = self._get_fingerprints(frags_flatten)
        
        sims = np.zeros((len(frags_flatten), n_bbs), dtype=np.float16)
        for start in range(0, n_bbs, bb_chunk_size):
            end = min(start + bb_chunk_size, n_bbs)
            batch_fps = bb_fps[start:end]
            batch_sizes = bb_sizes[start:end]
            
            delta = batch_sizes[None, :] - frag_sizes
            weights = 1 - np.clip(delta, 0, None) / batch_sizes[None, :]
            
            sims[:, start:end] = (weights * utils.get_batch_tversky_sims(frag_fps, batch_fps)).astype(np.float16)
        
        if self.max_bbs_per_frag > 0:
            kth = np.argpartition(-sims, self.max_bbs_per_frag-1, axis=1)[:, :self.max_bbs_per_frag]
            mask = np.zeros_like(sims, dtype=bool)
            rows = np.arange(sims.shape[0])[:, None]
            mask[rows, kth] = True
        else:
            mask = sims >= self.sim_threshold

        masks_per_comp = [
            mask[offsets[i]:offsets[i+1], :] for i in range(len(self._compositions))
        ]

        orig_comps = self._compositions
        self._compositions = [
            CompositionWithBBs(
                comp=comp,
                fragment_bbs=tuple(
                    [bb for bb, keep in zip(bb_mols, row) if keep] for row in comp_mask
                ))
            for comp, comp_mask in zip(orig_comps, masks_per_comp)
        ]

    def _process_sharded_building_blocks(self, bb_chunk_size: int=1000) -> None:
        """Keep global top-k candidates while streaming Molport SDF shards.

        This intentionally requires a positive ``max_bbs_per_frag``: an
        unbounded threshold result would again put the entire catalog in memory.
        The web service enforces a positive value for shared requests.
        """
        if self.max_bbs_per_frag <= 0:
            raise ValueError(
                "Streaming building-block sources require max_bbs_per_frag to be positive"
            )

        frag_lists = [path.fragments for path in self._compositions]
        offsets = np.concatenate(([0], np.cumsum([len(frag_list) for frag_list in frag_lists])))
        frags = [frag for frag_list in frag_lists for frag in frag_list]
        if not frags:
            return
        frag_sizes = np.array([frag.GetNumHeavyAtoms() for frag in frags])[:, None]
        frag_fps = self._get_fingerprints(frags)
        # Entries include a sequence number, so tied scores never compare BBs.
        heaps: List[List[Tuple[float, int, BuildingBlock]]] = [[] for _ in frags]
        sequence = 0

        for batch in _chunked(self._bb_repo.iter_bbs_for_reactions(self.reactions), bb_chunk_size):
            batch_sizes = np.array([bb.num_heavy_atoms for bb in batch])
            for bb in batch:
                bb.fingerprint = self._fp_generator.GetFingerprint(bb.mol)
            batch_fps = [bb.fingerprint for bb in batch]
            delta = batch_sizes[None, :] - frag_sizes
            weights = 1 - np.clip(delta, 0, None) / batch_sizes[None, :]
            scores = weights * utils.get_batch_tversky_sims(frag_fps, batch_fps)

            for frag_idx, row in enumerate(scores):
                heap = heaps[frag_idx]
                for bb_idx, score in enumerate(row):
                    entry = (float(score), sequence, batch[bb_idx])
                    sequence += 1
                    if len(heap) < self.max_bbs_per_frag:
                        heapq.heappush(heap, entry)
                    elif score > heap[0][0]:
                        heapq.heapreplace(heap, entry)
            # Only retained candidates keep a lazily reconstructable molecule.
            retained_ids = {id(entry[2]) for heap in heaps for entry in heap}
            for bb in batch:
                if id(bb) not in retained_ids:
                    bb.evict()

        selected_per_fragment = [
            [entry[2] for entry in sorted(heap, key=lambda entry: entry[0], reverse=True)]
            for heap in heaps
        ]
        original_comps = self._compositions
        self._compositions = [
            CompositionWithBBs(
                comp=comp,
                fragment_bbs=tuple(selected_per_fragment[offsets[i]:offsets[i + 1]]),
            )
            for i, comp in enumerate(original_comps)
        ]

    @staticmethod
    def _serialise_candidate(score: float, bb: BuildingBlock) -> Dict[str, Any]:
        """Return the small, JSON-safe representation used between shard tasks."""
        return {"score": float(score), "smiles": bb.get_smiles(), "props": bb.props}

    @staticmethod
    def _deserialise_candidate(candidate: Dict[str, Any]) -> BuildingBlock:
        mol = Chem.MolFromSmiles(candidate["smiles"])
        if mol is None:
            raise ValueError("Invalid building-block SMILES in shard result")
        bb = BuildingBlock(mol)
        for name, value in candidate.get("props", {}).items():
            bb.SetProp(name, value)
        return bb

    @staticmethod
    def _top_unique_index_rows(index, rows: np.ndarray, scores: np.ndarray, limit: int) -> List[Tuple[float, int, str]]:
        """Return the exact, deterministic top-k distinct-SMILES index rows.

        Numeric selection keeps the hot path in NumPy.  SMILES are read only
        for a small over-sampled frontier; ties at the final score are expanded
        so ordering remains identical to the previous exhaustive Python loop.
        """
        if not len(rows) or limit <= 0:
            return []
        frontier_size = min(len(rows), max(limit * 4, 256))
        while True:
            positions = np.argpartition(-scores, frontier_size - 1)[:frontier_size]
            frontier_rows = rows[positions]
            frontier_scores = scores[positions]
            order = np.lexsort((frontier_rows, index.smiles[frontier_rows], -frontier_scores))
            unique_count = len({str(index.smiles[int(row)]) for row in frontier_rows[order]})
            if unique_count >= limit or frontier_size == len(rows):
                break
            frontier_size = min(len(rows), frontier_size * 2)

        # Scores at the boundary may have been excluded by argpartition.  They
        # must be included to apply the existing score-then-SMILES tie break.
        seen: set[str] = set()
        provisional = []
        for position in order:
            row = int(frontier_rows[position])
            smiles = str(index.smiles[row])
            if smiles not in seen:
                seen.add(smiles)
                provisional.append((float(frontier_scores[position]), row, smiles))
                if len(provisional) == limit:
                    break
        cutoff = provisional[-1][0]
        positions = np.flatnonzero(scores >= cutoff)
        candidate_rows = rows[positions]
        candidate_scores = scores[positions]
        order = np.lexsort((candidate_rows, index.smiles[candidate_rows], -candidate_scores))
        selected: List[Tuple[float, int, str]] = []
        seen.clear()
        for position in order:
            row = int(candidate_rows[position])
            smiles = str(index.smiles[row])
            if smiles not in seen:
                seen.add(smiles)
                selected.append((float(candidate_scores[position]), row, smiles))
                if len(selected) == limit:
                    break
        return selected

    def select_molport_shard_candidates(self, shard_name: str, bb_chunk_size: int = 1000) -> List[List[Dict[str, Any]]]:
        """Select deterministic local top-k candidates for one Molport shard.

        The global top-k is contained in the union of every shard's local
        top-k.  This is the bounded intermediate representation used by the
        request-driven fan-out workers.
        """
        if not isinstance(self._bb_repo, ShardedBBRepository):
            raise ValueError("Shard selection is only available for Molport")
        if self.max_bbs_per_frag <= 0:
            raise ValueError("Streaming building-block sources require max_bbs_per_frag to be positive")

        self._process_query_mol()
        fragments = [frag for path in self._compositions for frag in path.fragments]
        if not fragments:
            return []
        frag_sizes = np.array([frag.GetNumHeavyAtoms() for frag in fragments])[:, None]
        frag_fps = self._get_fingerprints(fragments)
        shard_paths = [path for path in self._bb_repo.shard_paths() if path.name == shard_name]
        if not shard_paths:
            raise ValueError(f"Unknown Molport shard: {shard_name}")
        index = self._bb_repo.open_index(shard_paths[0])
        if index is not None:
            started = time.perf_counter()
            rows = index.eligible_rows(reaction.name for reaction in self.reactions)
            selected_rows = [
                self._top_unique_index_rows(
                    index, rows, index.score_row(query_fp, float(frag_sizes[fragment_index, 0]), rows),
                    self.max_bbs_per_frag,
                )
                for fragment_index, query_fp in enumerate(frag_fps)
            ]
            logger.warning(
                "Molport index selection completed: shard=%s eligible=%d elapsed_seconds=%.3f",
                shard_name, len(rows), time.perf_counter() - started,
            )
            return [
                [self._serialise_candidate(score, index.building_block(row)) for score, row, _ in pool]
                for pool in selected_rows
            ]

        selected: List[List[Tuple[float, BuildingBlock]]] = [[] for _ in fragments]

        for batch in _chunked(
            self._bb_repo.iter_bbs_for_reactions(self.reactions, shard_name=shard_name),
            bb_chunk_size,
        ):
            batch_sizes = np.array([bb.num_heavy_atoms for bb in batch])
            for bb in batch:
                bb.fingerprint = self._fp_generator.GetFingerprint(bb.mol)
            delta = batch_sizes[None, :] - frag_sizes
            weights = 1 - np.clip(delta, 0, None) / batch_sizes[None, :]
            scores = weights * utils.get_batch_tversky_sims(frag_fps, [bb.fingerprint for bb in batch])
            for fragment_index, row in enumerate(scores):
                pool = selected[fragment_index]
                by_smiles = {candidate.get_smiles(): index for index, (_, candidate) in enumerate(pool)}
                for bb_index, score in enumerate(row):
                    bb = batch[bb_index]
                    score_value = float(score)
                    existing = by_smiles.get(bb.get_smiles())
                    if existing is not None:
                        if score_value > pool[existing][0]:
                            pool[existing] = (score_value, bb)
                        continue
                    pool.append((score_value, bb))
                    pool.sort(key=lambda item: (-item[0], item[1].get_smiles()))
                    if len(pool) > self.max_bbs_per_frag:
                        _, evicted = pool.pop()
                        evicted.evict()
                    by_smiles = {candidate.get_smiles(): index for index, (_, candidate) in enumerate(pool)}
            retained_ids = {id(candidate) for pool in selected for _, candidate in pool}
            for bb in batch:
                if id(bb) not in retained_ids:
                    bb.evict()

        return [
            [self._serialise_candidate(score, bb) for score, bb in pool]
            for pool in selected
        ]

    def enumerate_from_molport_candidates(
        self,
        candidates_per_fragment: List[List[Dict[str, Any]]],
        max_evals_per_comp: Optional[int] = None,
        max_products_per_comp: Optional[int] = None,
        max_total_products: Optional[int] = None,
    ) -> None:
        """Enumerate after fan-out has already selected global Molport top-k."""
        self._process_query_mol()
        original_comps = self._compositions
        total_fragments = sum(len(comp.fragments) for comp in original_comps)
        if len(candidates_per_fragment) != total_fragments:
            raise ValueError("Shard results do not match the generated compositions")
        offset = 0
        self._compositions = []
        for comp in original_comps:
            count = len(comp.fragments)
            self._compositions.append(
                CompositionWithBBs(
                    comp=comp,
                    fragment_bbs=tuple(
                        [self._deserialise_candidate(candidate) for candidate in candidates]
                        for candidates in candidates_per_fragment[offset:offset + count]
                    ),
                )
            )
            offset += count
        self.enumerated_molecules = [EnumerationRecord(product=self.query_mol, bbs=[], reaction_names=[], props={})]
        self.enumerated_molecules += self._enumerate_base(
            max_evals_per_comp, max_products_per_comp, max_total_products
        )


class FragmentHEALER(MoleculeHEALER):
    '''
        Fragment HEALER: a specialized version of MoleculeHEALER that allows 
        fragment inputs that will be used as compositions for enumeration.
    '''
    def __init__(
            self, 
            bb_source: str = 'US_stock', 
            reaction_tags: list[str] = ['amide coupling', 'amide', 'C-N bond formation', 'C-N',
                                      'alkylation', 'N-arylation', 'azole', 'amination'],
            bb_repository: Optional[BBRepository] = None,
            shuffle_bb_order: bool = False,
            sim_threshold: float = 0.5,
            max_bbs_per_frag: int = -1,
            verbose: int = 1,
    ):
        '''
            Initialize FragmentHEALER.

            Args:
                bb_source: one of "US_stock", "EU_stock" or "Global_stock"; or path to an SDF file.
                reaction_tags: list of tags or 'all'.
                bb_repository: optional pre-loaded BBRepository for sharing across instances.
                shuffle_bb_order: whether to shuffle the order of building blocks.
                sim_threshold: similarity threshold for filtering building blocks.
                max_bbs_per_frag: maximum number of building blocks per fragment.
                    If <= 0, all building blocks will be considered. Otherwise, the similarity
                    threshold will be adjusted to the number of building blocks.
                verbose: verbosity level, 0 for errors, 1 for warnings, 2 for info.
        '''
        super().__init__(
            bb_source, reaction_tags, bb_repository, shuffle_bb_order,
            sim_threshold, max_bbs_per_frag, verbose
        )

    def set_query_mol(
        self, query_mol: Union[str, Chem.Mol, tuple[Chem.Mol, ...], tuple[str, ...]], 
    ) -> None:
        '''
            Set the query molecule for enumeration. The query can be a molecule with 
            multiple fragments or a tuple of molecules. 
        '''
        if isinstance(query_mol, str):
            query_mol = Chem.MolFromSmiles(query_mol, sanitize=False)
        elif isinstance(query_mol, tuple):
            if isinstance(query_mol[0], str):
                query_mol = tuple(Chem.MolFromSmiles(smi, sanitize=False) for smi in query_mol)
            mol_out = query_mol[0]
            for m in query_mol[1:]:
                mol_out = Chem.CombineMols(mol_out, m)
            query_mol = mol_out
        self.query_mol = query_mol
        
        frags = Chem.GetMolFrags(query_mol, sanitizeFrags=False)
        if len(frags) < 2:
            raise ValueError('Query molecule must have at least two fragments. '
                             'Use MoleculeHEALER for single-component molecules.')
        
        flag = Chem.SanitizeMol(self.query_mol, catchErrors=True)
        assert flag == Chem.rdmolops.SanitizeFlags.SANITIZE_NONE, f"SanitizeMol failed: {flag}"

        self._compositions = []     # reset compositions
        
    def _process_query_mol(self) -> None:
        '''
            Process the query molecule to generate compositions based on the 
            custom split sites if provided, or create retrosynthesis tree 
            with all possible compositions.
        '''
        if not isinstance(self.query_mol, Chem.Mol):
            raise ValueError('Query molecule must be an RDKit Mol object. '
                             'Set it using set_query_mol() method.')
        
        frags = Chem.GetMolFrags(self.query_mol, asMols=True, sanitizeFrags=True)
        self._compositions.append(
            CompositionPath(fragments=frags)
        )

        logger.debug("Generated %d composition(s):\n%s", len(self._compositions), self._composition_prints())
