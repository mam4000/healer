'''
    Interface utilities for HEALER classes to standardize web app interactions.
    Adapted for the internal web package.
'''
import os
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Union, Tuple

from rdkit import Chem

from healer.application.healer import MoleculeHEALER, SiteHEALER, FragmentHEALER


logger = logging.getLogger(__name__)

# Reactions always come from package data
HEALER_PKG = Path(__file__).parent.parent
REACTIONS_PATH = HEALER_PKG / 'data' / 'reactions' / 'reactions.json'

# Building blocks can be overridden via HEALER_DATA_DIR
_env_data_dir = os.environ.get('HEALER_DATA_DIR')
if _env_data_dir:
    BB_BASE_PATH = Path(_env_data_dir)
else:
    BB_BASE_PATH = HEALER_PKG / 'data' / 'buildingblocks'

# Named short-keys usable in both CLI and the web dropdown.
# Keys match bb_repository.py; subdirectory names are the actual dir names on disk.
_BB_NAMED_SOURCES: Dict[str, Dict[str, str]] = {
    "US_stock":     {"subdir": "Enamine_Rush-Delivery_Building_Blocks-US", "label": "Enamine US Stock"},
    "EU_stock":     {"subdir": "Enamine_Rush-Delivery_Building_Blocks-EU", "label": "Enamine EU Stock"},
    "Global_stock": {"subdir": "Enamine_Building_Blocks_Stock",            "label": "Enamine Global Stock"},
    "test":         {"subdir": None,                                        "label": "Test Set (100 BBs)"},
    "molport_full": {"subdir": "Molport_Full_Database",                    "label": "Molport Full Database"},
}

# Fallback pretty-name lookup for any extra SDF files found by rglob
# (keyed on the stem without "_processed")
_EXTRA_PRETTY_NAMES: Dict[str, str] = {}

SERVER_MODE = os.environ.get('HEALER_SERVER_MODE', 'false').lower() == 'true'

# Default limits for server mode (can be overridden via env vars). These defaults
# are deliberately conservative for a shared, CPU-bound web service.
SERVER_LIMITS = {
    'max_evals_per_comp': int(os.environ.get('HEALER_LIMIT_MAX_EVALS', 2000)),
    'max_products_per_comp': int(os.environ.get('HEALER_LIMIT_MAX_PRODUCTS', 100)),
    'max_total_products': int(os.environ.get('HEALER_LIMIT_MAX_TOTAL', 500)),
    'sim_threshold_min': float(os.environ.get('HEALER_LIMIT_SIM_MIN', 0.5)),
    'sim_threshold_max': float(os.environ.get('HEALER_LIMIT_SIM_MAX', 1.0)),
    'max_bbs_per_frag': int(os.environ.get('HEALER_LIMIT_MAX_BBS', 10)),
    'n_compositions_max': int(os.environ.get('HEALER_LIMIT_N_COMP', 10)),
    'retro_depth_max': int(os.environ.get('HEALER_LIMIT_RETRO_DEPTH', 1)),
    'min_frag_size_min': int(os.environ.get('HEALER_LIMIT_MIN_FRAG', 7)),
    'max_reaction_tags': int(os.environ.get('HEALER_LIMIT_MAX_RXN_TAGS', 8)),
}


def get_server_limits() -> Dict[str, Any]:
    """Return server limits configuration."""
    return SERVER_LIMITS.copy()


def apply_server_limits(params: Dict[str, Any], healer_type: str = "molecule") -> Dict[str, Any]:
    """Apply non-bypassable parameter limits when serving shared users."""
    if not SERVER_MODE:
        return params
    
    limited = params.copy()
    
    for key, limit_key in (
        ('max_evals_per_comp', 'max_evals_per_comp'),
        ('max_products_per_comp', 'max_products_per_comp'),
        ('max_total_products', 'max_total_products'),
    ):
        # None previously meant unlimited, which let clients bypass the limit.
        value = limited.get(key)
        limited[key] = SERVER_LIMITS[limit_key] if value is None else min(value, SERVER_LIMITS[limit_key])

    tags = [tag for tag in limited.get('reaction_tags', []) if tag.strip()]
    if any(tag.lower() == 'all' for tag in tags):
        raise ValueError("The 'all' reaction tag is unavailable in server mode")
    limited['reaction_tags'] = tags[:SERVER_LIMITS['max_reaction_tags']]
    
    if healer_type in ('molecule', 'fragment'):
        if 'sim_threshold' in limited:
            limited['sim_threshold'] = max(SERVER_LIMITS['sim_threshold_min'], 
                                           min(limited['sim_threshold'], SERVER_LIMITS['sim_threshold_max']))
        
        if 'max_bbs_per_frag' in limited:
            if limited['max_bbs_per_frag'] < 0 or limited['max_bbs_per_frag'] > SERVER_LIMITS['max_bbs_per_frag']:
                limited['max_bbs_per_frag'] = SERVER_LIMITS['max_bbs_per_frag']
        
        if 'n_compositions' in limited:
            limited['n_compositions'] = min(limited['n_compositions'], SERVER_LIMITS['n_compositions_max'])
        
        if 'retro_tree_depth' in limited:
            limited['retro_tree_depth'] = min(limited['retro_tree_depth'], SERVER_LIMITS['retro_depth_max'])
        
        if 'min_frag_size' in limited:
            limited['min_frag_size'] = max(limited['min_frag_size'], SERVER_LIMITS['min_frag_size_min'])
    
    return limited


def discover_building_blocks() -> List[Dict[str, str]]:
    """
    Discover available processed building block files under BB_BASE_PATH.

    Returns:
        List of dicts with:
          'value' — absolute path to the SDF file (passed as bb_source to enumeration)
          'label' — human-readable display name
          'key'   — short named key (e.g. "US_stock"), present only for known sources
    """
    if not BB_BASE_PATH.exists():
        logger.warning(f"Building blocks directory not found: {BB_BASE_PATH}")
        return []

    seen_paths: set = set()
    options: List[Dict[str, str]] = []

    # 1. Walk named sources first so they appear at the top with their pretty labels
    for key, info in _BB_NAMED_SOURCES.items():
        subdir = info["subdir"]
        if subdir is None:
            # e.g. "test" — look for *_processed.sdf directly in BB_BASE_PATH
            matches = sorted(BB_BASE_PATH.glob("*_processed.sdf"))
        else:
            target_dir = BB_BASE_PATH / subdir
            if not target_dir.exists():
                continue
            matches = sorted(target_dir.glob("*_processed.sdf"))

        if key == "molport_full":
            if matches:
                # The value is a logical source key, not a particular shard:
                # the worker opens one matching SDF at a time.
                options.append({"value": key, "label": info["label"], "key": key})
                seen_paths.update(str(sdf_path.resolve()) for sdf_path in matches)
            continue

        for sdf_path in matches:
            abs_path = str(sdf_path.resolve())
            if abs_path in seen_paths:
                continue
            seen_paths.add(abs_path)
            options.append({"value": abs_path, "label": info["label"], "key": key})

    # 2. Catch any remaining *_processed.sdf files not covered by named sources
    for sdf_path in sorted(BB_BASE_PATH.rglob("*_processed.sdf")):
        abs_path = str(sdf_path.resolve())
        if abs_path in seen_paths:
            continue
        seen_paths.add(abs_path)
        stem = sdf_path.stem.replace("_processed", "")
        label = _EXTRA_PRETTY_NAMES.get(stem, stem.replace("_", " "))
        options.append({"value": abs_path, "label": label})

    return options


def resolve_bb_path(bb_source: str) -> str:
    """
    Resolve a bb_source to an absolute SDF file path.

    Accepts:
      - A named short-key ("US_stock", "EU_stock", "Global_stock", "test")
      - An absolute path (returned as-is after existence check)
      - A path relative to BB_BASE_PATH
    """
    # Named key → delegate to bb_repository which already handles glob resolution
    if bb_source in _BB_NAMED_SOURCES:
        from healer.domain.bb_repository import resolve_bb_path as _repo_resolve
        return _repo_resolve(bb_source)

    # Absolute path
    p = Path(bb_source)
    if p.is_absolute():
        if not p.exists():
            raise FileNotFoundError(f"Building block file not found: {p}")
        return str(p)

    # Relative path — resolve against BB_BASE_PATH
    candidate = BB_BASE_PATH / bb_source
    if candidate.exists():
        return str(candidate)

    raise FileNotFoundError(
        f"Building block source not found: {bb_source!r} "
        f"(looked in {BB_BASE_PATH})"
    )


def count_molecular_fragments(smiles: str) -> int:
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return 0
        fragments = Chem.GetMolFrags(mol, asMols=True)
        return len(fragments)
    except Exception as e:
        logger.error(f"Error counting fragments in {smiles}: {e}")
        return 0


def create_molecule_healer(
    bb_source: str = 'test',
    reaction_tags: List[str] = None,
    sim_threshold: float = 0.15,
    max_bbs_per_frag: int = -1,
    verbose: int = 1,
    shuffle_bb_order: bool = False,
    use_fragment_healer: bool = False
) -> Union[MoleculeHEALER, FragmentHEALER]:
    
    if reaction_tags is None:
        reaction_tags = ["amide coupling", "amide", "C-N bond formation", "C-N",
                        "alkylation", "N-arylation", "azole", "amination"]

    bb_path = resolve_bb_path(bb_source)
    
    common_kwargs = {
        'bb_source': bb_path,
        'reaction_tags': reaction_tags,
        'shuffle_bb_order': shuffle_bb_order,
        'sim_threshold': sim_threshold,
        'max_bbs_per_frag': max_bbs_per_frag,
        'verbose': verbose
    }
    
    if use_fragment_healer:
        return FragmentHEALER(**common_kwargs)
    else:
        return MoleculeHEALER(**common_kwargs)


def create_site_healer(
    bb_source: str = 'test',
    reaction_tags: List[str] = None,
    rules: Dict[str, Tuple[int, int]] = None,
    struct_rules: List[str] = None,
    verbose: int = 1,
    shuffle_bb_order: bool = False
) -> SiteHEALER:
    
    if reaction_tags is None:
        reaction_tags = ["amide coupling", "amide", "C-N bond formation", "C-N",
                        "alkylation", "N-arylation", "azole", "amination"]
    
    if rules is None:
        rules = {
            'MW': (0, 100), 'HBD': (0, 5), 'HBA': (0, 5), 'TPSA': (0, 100),
            'RotB': (0, 10), 'Rings': (0, 10), 'ArRings': (0, 5), 'Chiral': (0, 5),
        }
    
    if struct_rules is None:
        struct_rules = []
    
    bb_path = resolve_bb_path(bb_source)
    
    return SiteHEALER(
        bb_source=bb_path,
        reaction_tags=reaction_tags,
        rules=rules,
        struct_rules=struct_rules,
        max_bbs=SERVER_LIMITS['max_bbs_per_frag'] if SERVER_MODE else 10,
        shuffle_bb_order=shuffle_bb_order,
        verbose=verbose
    )


def run_molecule_enumeration(
    molecule: str,
    bb_source: str,
    reaction_tags: List[str],
    custom_sites: Optional[List[Tuple[int, int]]] = None,
    sim_threshold: float = 0.15,
    n_compositions: int = 10,
    randomize_compositions: bool = False,
    random_seed: int = -1,
    retro_tree_depth: int = 1,
    min_frag_size: int = 3,
    max_bbs_per_frag: int = -1,
    shuffle_bb_order: bool = False,
    max_evals_per_comp: Optional[int] = None,
    max_products_per_comp: Optional[int] = None,
    max_total_products: Optional[int] = None,
    use_fragment_healer: bool = False
) -> List[Dict[str, Any]]:
    
    try:
        num_fragments = count_molecular_fragments(molecule)
        auto_use_fragment_healer = num_fragments > 1
        final_use_fragment_healer = use_fragment_healer or auto_use_fragment_healer
        
        healer = create_molecule_healer(
            bb_source=bb_source,
            reaction_tags=reaction_tags,
            sim_threshold=sim_threshold,
            max_bbs_per_frag=max_bbs_per_frag,
            verbose=1,
            shuffle_bb_order=shuffle_bb_order,
            use_fragment_healer=final_use_fragment_healer
        )
        
        if final_use_fragment_healer:
            healer.set_query_mol(query_mol=molecule)
        else:
            healer.set_query_mol(
                query_mol=molecule,
                n_compositions=n_compositions,
                randomize_compositions=randomize_compositions,
                random_seed=random_seed,
                custom_split_sites=[custom_sites] if custom_sites else None,
                retro_tree_depth=retro_tree_depth,
                min_frag_size=min_frag_size
            )

        healer.enumerate(
            max_evals_per_comp=max_evals_per_comp,
            max_products_per_comp=max_products_per_comp,
            max_total_products=max_total_products
        )
        return healer.get_results(as_dict=True, calc_similarity=True, calc_properties=True)
        
    except Exception as e:
        logger.error(f"Error in molecule enumeration: {str(e)}")
        raise


def run_site_enumeration(
    molecule: str,
    bb_source: str,
    reaction_tags: List[str],
    reactive_sites: Optional[List[int]] = None,
    rules: Dict[str, Tuple[int, int]] = None,
    struct_rules: List[str] = None,
    shuffle_bb_order: bool = False,
    max_evals_per_comp: Optional[int] = None,
    max_products_per_comp: Optional[int] = None,
    max_total_products: Optional[int] = None
) -> List[Dict[str, Any]]:
    
    try:
        healer = create_site_healer(
            bb_source=bb_source,
            reaction_tags=reaction_tags,
            rules=rules,
            struct_rules=struct_rules,
            shuffle_bb_order=shuffle_bb_order,
            verbose=1
        )
        
        healer.set_query_mol(
            query_mol=molecule,
            reactive_sites=reactive_sites
        )

        healer.enumerate(
            max_evals_per_comp=max_evals_per_comp,
            max_products_per_comp=max_products_per_comp,
            max_total_products=max_total_products
        )
        return healer.get_results(as_dict=True, calc_similarity=True, calc_properties=True)
        
    except Exception as e:
        logger.error(f"Error in site enumeration: {str(e)}")
        raise

def format_enumeration_results(results: List[Dict[str, Any]], app_type: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    display_results = []
    complete_results = []
    
    for result in results:
        complete_result = result.copy()
        complete_results.append(complete_result)
        
        display_result = {
            'Product': result.get('Product', ''),
            'Similarity_to_query': result.get('Similarity_to_query', 0.0),
            'QED': result.get('qed', 0.0)
        }
        
        if 'stoplight_color' in result:
            display_result['stoplight_color'] = result['stoplight_color']
        
        bb_keys = [k for k in result.keys() if k.startswith('BB')]
        bb_keys.sort(key=lambda x: int(x[2:]))
        
        if app_type == 'molecule':
            for i, bb_key in enumerate(bb_keys, 1):
                if result.get(bb_key):
                    display_result[f'BB{i}'] = result[bb_key]
                    url_key = f'URL{i}'
                    if result.get(url_key):
                        display_result[url_key] = result[url_key]
        elif app_type == 'site':
            if bb_keys and result.get(bb_keys[1]):
                display_result['BB'] = result[bb_keys[1]]
                if result.get('URL2'):
                    display_result['URL'] = result['URL2']
        
        rxn_keys = [k for k in result.keys() if k.startswith('Reaction') and k.endswith('_name')]
        if rxn_keys:
            rxn_keys.sort(key=lambda x: int(x.split('_')[0][8:]))
            reaction_names = [result.get(k, '') for k in rxn_keys if result.get(k)]
            if reaction_names:
                display_result['Reaction_name'] = ' -> '.join(reaction_names)
        
        display_results.append(display_result)
    
    return display_results, complete_results
