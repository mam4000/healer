'''
    Pydantic schemas for HEALER web application requests and responses.
'''
from typing import List, Optional, Tuple, Dict, Any

from pydantic import BaseModel, Field


class MoleculeRequest(BaseModel):
    molecule: str = Field(..., description="SMILES string of the query molecule")
    bb_source: str = Field("test", description="Building block source (e.g., 'test', 'US_stock')")
    reaction_tags: List[str] = Field(
        default=["amide coupling", "amide", "C-N bond formation", "C-N",
                 "alkylation", "N-arylation", "azole", "amination"],
        description="List of reaction tags to filter reactions"
    )
    custom_sites: Optional[List[Tuple[int, int]]] = Field(
        None, description="Custom split sites as list of (atom_idx1, atom_idx2) tuples"
    )
    sim_threshold: float = Field(0.15, description="Similarity threshold for BB selection")
    n_compositions: int = Field(10, description="Number of compositions to generate")
    randomize_compositions: bool = Field(False, description="Whether to randomize composition order")
    random_seed: int = Field(-1, description="Random seed (-1 for no seed)")
    retro_tree_depth: int = Field(1, description="Depth of retrosynthesis tree")
    min_frag_size: int = Field(3, description="Minimum fragment size")
    
    # New/Renamed fields
    max_bbs_per_frag: int = Field(-1, description="Max building blocks per fragment (-1 for unlimited)")
    shuffle_bb_order: bool = Field(False, description="Shuffle building block order")
    
    # Limits
    max_evals_per_comp: Optional[int] = Field(None, description="Max reaction attempts per composition")
    max_products_per_comp: Optional[int] = Field(None, description="Max products per composition")
    max_total_products: Optional[int] = Field(None, description="Max total products")
    
    use_fragment_healer: bool = Field(False, description="Force use of FragmentHEALER")

class SiteRequest(BaseModel):
    molecule: str = Field(..., description="SMILES string of the query molecule")
    bb_source: str = Field("test", description="Building block source")
    reaction_tags: List[str] = Field(
        default=["amide coupling", "amide", "C-N bond formation", "C-N",
                 "alkylation", "N-arylation", "azole", "amination"],
        description="List of reaction tags"
    )
    reactive_sites: Optional[List[int]] = Field(None, description="List of reactive atom indices")
    rules: Optional[Dict[str, Tuple[int, int]]] = Field(
        default={
            'MW': (0, 500),
            'HBD': (0, 5),
            'HBA': (0, 10),
            'TPSA': (0, 200),
            'RotB': (0, 10),
            'Rings': (0, 10),
            'ArRings': (0, 5),
            'Chiral': (0, 5),
        },
        description="Property rules (min, max)"
    )
    struct_rules: Optional[List[str]] = Field(default=[], description="SMARTS patterns for structure rules")
    
    shuffle_bb_order: bool = Field(False, description="Shuffle building block order")
    
    # Limits
    max_evals_per_comp: Optional[int] = Field(None, description="Max reaction attempts per composition")
    max_products_per_comp: Optional[int] = Field(None, description="Max products per composition")
    max_total_products: Optional[int] = Field(None, description="Max total products")

class JobSubmitResponse(BaseModel):
    job_id: str
    status: str

class JobResult(BaseModel):
    display: List[Dict[str, Any]]
    complete: List[Dict[str, Any]]

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: Optional[JobResult] = None
    error: Optional[str] = None
    completed_shards: Optional[int] = None
    total_shards: Optional[int] = None
    phase: Optional[str] = None
