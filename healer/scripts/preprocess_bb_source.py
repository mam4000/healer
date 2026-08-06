'''
    This script implements the preprocessing of the Building Block datasets.
'''

import io
import json
import zipfile
from itertools import islice
from pathlib import Path
from joblib import Parallel, delayed
import healer.utils.utils as utils

from tqdm import tqdm
from rdkit import Chem
from rdkit.Chem import SDMolSupplier, ForwardSDMolSupplier
from healer.domain.molport_index import build_shard_index

# Get reactions path from package data
_HEALER_PKG = Path(__file__).parent.parent
_REACTIONS_FILE = _HEALER_PKG / 'data' / 'reactions' / 'reactions.json'

REACTIONS = utils.load_reactions_from_json(str(_REACTIONS_FILE))
REACTIONS = [rxn for rxn in REACTIONS if rxn.is_valid()]

_CHUNK_SIZE = 200  # mols per worker call - balances IPC overhead vs memory


def _ichunk(iterable, size: int):
    """Yield successive fixed-size lists from an iterable."""
    it = iter(iterable)
    while chunk := list(islice(it, size)):
        yield chunk


def _iter_sdf_records(sdf_file: str):
    """Yield raw SDF record strings (one per molecule) by splitting on 4 dollar signs.
    Sending plain strings across process boundaries avoids RDKit Mol pickle
    issues where string properties are silently dropped.
    """
    record: list[str] = []
    with open(sdf_file) as f:
        for line in f:
            record.append(line)
            if line.startswith('$$$$'):
                yield ''.join(record)
                record = []


def add_rxn_annotations(mol: Chem.Mol) -> Chem.Mol:
    """
        Add a new property called 'rxn_annotations' to the molecule.
        The propery is a dictionary where the keys are the reaction
        names in which the molecule can be found and the values are the
        positions of the molecule in the reaction. Example:
        ```
            {
                'amide coupling-1': [0, 1],
                'sulfoxide': [1],
            }
        ```

        Args:
            mol (Chem.Mol): The molecule to which the property will be added.

        Returns:
            Chem.Mol: The molecule with the new property added.
    """
    rxn_annotations = {}
    for rxn in REACTIONS:
        idx = rxn.get_reactant_index(mol)
        if idx is not None:
            rxn_annotations[rxn.name] = idx
    mol.SetProp('rxn_annotations', json.dumps(rxn_annotations))
    
    return mol

def remove_smaller_fragments(mol: Chem.Mol) -> Chem.Mol:
    """
        Remove the smaller fragments from the molecule.

        Args:
            mol (Chem.Mol): The molecule to process.

        Returns:
            Chem.Mol: The processed molecule.
    """
    frags = Chem.GetMolFrags(mol, asMols=True)
    largest_frag = max(frags, key=lambda x: x.GetNumAtoms())
    return largest_frag

def extract_zip_if_needed(input_file: str, verbose: bool = True) -> str:
    """
        Extract ZIP file if the input is a ZIP file, otherwise return the input file path.
        
        Args:
            input_file (str): Path to the input file (could be ZIP or SDF).
            verbose (bool): Whether to print extraction progress.
            
        Returns:
            str: Path to the extracted SDF file or original file if not a ZIP.
    """
    input_path = Path(input_file)
    
    if input_path.suffix.lower() == '.zip':
        extract_dir = input_path.parent / input_path.stem
        extract_dir.mkdir(exist_ok=True)
        
        if verbose:
            print(f"Extracting {input_file} to {extract_dir}")
            
        with zipfile.ZipFile(input_file, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
            
        # Find the SDF file in the extracted directory
        sdf_files = list(extract_dir.glob('*.sdf'))
        if not sdf_files:
            raise FileNotFoundError(f"No SDF file found in extracted ZIP: {input_file}")
        
        if len(sdf_files) > 1:
            print(f"Warning: Multiple SDF files found. Using: {sdf_files[0]}")
            
        return str(sdf_files[0])
    
    return input_file

def _process_batch(sdf_blocks: list[str]) -> list[str]:
    """Process a batch of raw SDF record strings in one worker call."""
    return [r for block in sdf_blocks if (r := _process_mol(block)) is not None]


def _process_mol(sdf_block: str) -> str | None:
    """Parse a raw SDF block, process it, and return a serialized SDF record string.
    Accepts a plain string so nothing RDKit-specific is pickled across processes.
    Returns None if the molecule is invalid or matches no reactions.
    """
    # ForwardSDMolSupplier parses both the mol block and SDF property lines.
    # MolFromMolBlock only reads the connectivity table and drops SDF properties.
    suppl = ForwardSDMolSupplier(io.BytesIO(sdf_block.encode()), removeHs=False)
    mol = next(suppl, None)
    if mol is None:
        return None
    original_props = {name: mol.GetProp(name) for name in mol.GetPropNames()}
    mol = remove_smaller_fragments(mol)
    for name, val in original_props.items():
        mol.SetProp(name, val)
    mol = add_rxn_annotations(mol)
    annotations = json.loads(mol.GetProp('rxn_annotations'))
    if not annotations:
        return None
    block = Chem.MolToMolBlock(mol)
    for name in mol.GetPropNames():
        block += f'>  <{name}>\n{mol.GetProp(name)}\n\n'
    block += '$$$$\n'
    return block


def main(input_file: str, output_dir: str = None, verbose: bool = True,
         n_workers: int = 1) -> None:
    """
        Process the building block file and add the 'rxn_annotations' property
        to each molecule. Automatically extracts ZIP files if needed.

        Args:
            input_file (str): Path to the input file (SDF or ZIP containing SDF).
            output_dir (str): Directory to save the processed file. If None, saves
                              in the same directory as the input file.
            verbose (bool): Whether to print progress information.
            n_workers (int): Number of worker processes. Default 1 (sequential).
    """
    # Extract ZIP file if needed
    sdf_file = extract_zip_if_needed(input_file, verbose)
    
    # Determine output path
    sdf_path = Path(sdf_file)
    if output_dir:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_file = out_dir / (sdf_path.stem + '_processed.sdf')
    else:
        output_file = sdf_path.parent / (sdf_path.stem + '_processed.sdf')
    
    # Use SDMolSupplier only to get the total count for the progress bar
    total = len(SDMolSupplier(sdf_file))

    n_chunks = -(-total // _CHUNK_SIZE)  # ceiling division
    batches = Parallel(n_jobs=n_workers, return_as='generator')(
        delayed(_process_batch)(chunk) for chunk in _ichunk(_iter_sdf_records(sdf_file), _CHUNK_SIZE)
    )
    count = 0
    temporary_output = output_file.with_name(output_file.name + '.building')
    with open(str(temporary_output), 'w', buffering=8 * 1024 * 1024) as f:
        for batch in tqdm(batches, desc="Processing BBs", unit="batch",
                          total=n_chunks, disable=not verbose):
            count += len(batch)
            f.writelines(batch)

    # The SDF is complete before its index is built.  The index manifest is
    # published last, so request workers can only observe a complete artifact.
    temporary_output.replace(output_file)
    index_dir = build_shard_index(output_file, _REACTIONS_FILE)

    print(f"Processed {total} molecules, annotated {count} with reactions.")
    print(f"Output written to {output_file}")
    print(f"Search index written to {index_dir}")


def cli():
    """CLI entry point for preprocess-bb command."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="preprocess-bb",
        description="Preprocess building block files for HEALER."
    )
    parser.add_argument("input_file", type=str, help="Path to the input SDF or ZIP file.")
    parser.add_argument("-o", "--output-dir", type=str, default=None,
                        help="Output directory for processed file. Defaults to same directory as input.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output.")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Number of worker processes (default: 1). Use -1 for all cores.")
    args = parser.parse_args()
    main(args.input_file, output_dir=args.output_dir, verbose=args.verbose,
         n_workers=args.workers)


if __name__ == "__main__":
    cli()
