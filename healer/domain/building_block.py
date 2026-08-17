'''
    Wrapper for buildingblock molecules to parse the properties automatically.
'''
import json
from typing import Any, Dict, Optional
from rdkit import Chem
from rdkit.DataStructs.cDataStructs import ExplicitBitVect


class BuildingBlock:
    def __init__(self, molecule: Chem.Mol) -> None:
        '''
            Initialize the BuildingBlock with a molecule.
        '''
        self._smiles: str = Chem.MolToSmiles(molecule)
        self._mol: Optional[Chem.Mol] = None      # lazy, reconstructed on demand
        self.num_heavy_atoms: int = molecule.GetNumHeavyAtoms()
        self.fingerprint: Optional[ExplicitBitVect] = None
        self.props: Dict[str, Any] = {
            k: self._parse_value(v)
            for k, v in molecule.GetPropsAsDict().items()
        }

        # Preserve the original Mol if atoms carry properties that SMILES cannot round-trip.
        has_atom_props = any(atom.GetPropsAsDict() for atom in molecule.GetAtoms())
        self._mol_with_atom_props: Optional[Chem.Mol] = molecule if has_atom_props else None

    def __hash__(self) -> int:
        '''
            Hash the building block based on its SMILES representation.
        '''
        return hash(self._smiles)

    def __getattr__(self, attr: str) -> Any:
        '''
            Delegate attribute access to the underlying RDKit molecule.
            This allows us to access properties like GetNumAtoms, GetNumBonds, etc.
        '''
        # Prevent infinite recursion during pickle reconstruction
        if '_smiles' not in self.__dict__:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{attr}'")
        return getattr(self.mol, attr)

    def get_parsed_prop(self, name: str) -> Any:
        '''
            Fetch the parsed Python object for this property.
        '''
        return self.props.get(name, '')

    def get_url(self) -> str:
        """Return the supplier URL, including Molport's source field."""
        for property_name in ("URL", "PUBCHEM_EXT_SUBSTANCE_URL"):
            value = self.get_parsed_prop(property_name)
            if isinstance(value, str) and value:
                return value
        return ""

    @property
    def mol(self) -> Chem.Mol:
        '''
            RDKit molecule object. Lazily reconstructed from SMILES
            if it has been evicted or not yet created.
        '''
        if self._mol_with_atom_props is not None:
            return self._mol_with_atom_props
        if self._mol is None:
            self._mol = Chem.MolFromSmiles(self._smiles)
        return self._mol
    
    def evict(self) -> None:
        '''
            Drop the cached Mol to free memory.
            It will be lazily reconstructed on next access of ``mol``.
        '''
        self._mol = None
        # _mol_with_atom_props is intentionally retained — atom properties
        # cannot be reconstructed from SMILES.

    def get_smiles(self) -> str:
        '''
            Get the canonical SMILES representation of the building block.
        '''
        return self._smiles
    
    def SetProp(self, name: str, value: Any) -> None:
        '''
            Set a property on the underlying Mol *and* update our parsed props.
        '''
        if not isinstance(value, str):
            raw = json.dumps(value)
        else:
            raw = value
        self.mol.SetProp(name, raw)
        self.props[name] = self._parse_value(raw)

    def ClearProp(self, name: str) -> None:
        '''
            Remove a property from the Mol and from parsed props.
        '''
        self.mol.ClearProp(name)
        self.props.pop(name, None)

    def _parse_value(self, val: str) -> Any:
        '''
            Parse a string value into a Python object.
        '''
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
