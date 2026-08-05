<p align="center">
  <img src="assets/healer_logo.png" alt="HEALER Logo" width="400"/>
</p>

<h1 align="center">HEALER</h1>
<h3 align="center">Hit Expansion to Advanced Leads Using Enumerated Reactions</h3>

<p align="center">
  <a href="#installation">Installation</a> •
  <a href="#building-blocks">Building Blocks</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#usage">Usage</a> •
  <a href="#web-interface">Web Interface</a> •
  <a href="#citation">Citation</a>
</p>

---

HEALER generates synthetically accessible molecular analogs by combining retrosynthetic fragmentation with commercially available building blocks and validated reaction templates. It bridges the gap between computational design and laboratory synthesis.

## Features

- **Molecule HEALER** — Retrosynthetically fragment a molecule and re-enumerate with similar building blocks
- **Fragment HEALER** — Enumerate from pre-fragmented molecules (multi-component SMILES)
- **Site HEALER** — Targeted enumeration at specific reactive sites with property filters
- **Synthetically Accessible** — All products use validated reaction templates
- **Flexible** — Works with any building block library in SDF format

## Installation

### Prerequisites

- Python ≥ 3.11
- [Conda](https://docs.conda.io/en/latest/miniconda.html) (recommended)

### Option 1: pip install (recommended)

```bash
pip install mol-healer
```

### Option 2: From source

```bash
git clone https://github.com/eneskelestemur/healer.git
cd healer

# Create conda environment
conda env create -f environment.yml
conda activate healer

# Install in development mode
pip install -e .
```

### Optional: Web interface

```bash
pip install mol-healer[web]
```

## Building Blocks

HEALER includes a small test set for demos. For production use, you'll need to set up building block libraries.

### Download & Process

1. Download building blocks (e.g., from [Enamine](https://enamine.net/building-blocks/building-blocks-catalog))

2. Preprocess to add reaction annotations:
   ```bash
   preprocess-bb ~/Downloads/Enamine_BBs.zip -o ~/.healer/buildingblocks/ --verbose
   ```

3. Set the data directory:
   ```bash
   export HEALER_DATA_DIR=~/.healer
   ```

### Custom Libraries

Any SDF file can be used as a building block source. Just preprocess it:

```bash
preprocess-bb my_custom_library.sdf -o ~/.healer/buildingblocks/
```

Then reference it by path:
```python
healer = MoleculeHEALER(bb_source='/path/to/my_custom_library_processed.sdf')
```

## Quick Start

### Python API

```python
from healer import MoleculeHEALER

# Initialize with building block source and reaction filters
healer = MoleculeHEALER(
    bb_source='test',  # Use test set or path to processed SDF
    reaction_tags=['amide coupling', 'N-arylation'],
    sim_threshold=0.5,
)

# Set query molecule and enumerate
healer.set_query_mol("CC1(C)SC2C(NC(=O)Cc3ccccc3)C(=O)N2C1C(=O)O", n_compositions=10)
healer.enumerate(max_evals_per_comp=500)

# Get results
results = healer.get_results(calc_similarity=True, calc_properties=True)
print(f"Generated {len(results)} analogs")
```

### Command Line

```bash
# Basic enumeration
healer molecule "CCO" --bb-source test -o results.csv

# View molecule with atom indices (for site specification)
healer view "c1ccccc1N"

# Site-specific enumeration
healer site "c1ccccc1N" --reactive-sites "[5]" --bb-source test

# Fragment-based enumeration
healer fragment "c1ccccc1.CC(=O)O" --bb-source test

# Parallel synthesis loop (uses all CPUs)
healer molecule input.csv --n-jobs -1 -o results.csv
```

## Contributing Reactions

We welcome contributions to expand our reaction library! Each reaction entry in `reactions.json` follows this format:

```json
{
  "reaction-name": {
    "description": "Brief description of the reaction mechanism",
    "long_name": "Full reaction name",
    "syn_smarts": "[reactant1].[reactant2]>>[product]",
    "retro_smarts": "[product]>>[fragment1].[fragment2]",
    "rhs_classes": ["bb-class-1", "bb-class-2"],
    "tags": ["reaction-type", "functional-group"],
    "tier": 1
  }
}
```

Key fields: `syn_smarts` (forward reaction), `retro_smarts` (retrosynthetic transform), `rhs_classes` (building block functional group classes), and `tags` (for filtering).

To contribute a reaction, please open a [GitHub issue](https://github.com/eneskelestemur/healer/issues) with your proposed reaction SMARTS, or contact enesk@email.unc.edu.

## Usage

### HEALER Classes

| Class | Use Case | Input |
|-------|----------|-------|
| `MoleculeHEALER` | Full retrosynthetic enumeration | Single molecule SMILES |
| `FragmentHEALER` | Enumeration from fragments | Dot-separated SMILES |
| `SiteHEALER` | Site-specific enumeration | Molecule + atom indices |

### Key Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| **Common (all modes)** | | |
| `bb_source` | Building block library (path to processed SDF) | `'test'` |
| `reaction_tags` | Filter reactions by tag (list or `'all'`) | `'all'` |
| `shuffle_bb_order` | Randomize building block order | `False` |
| `max_evals_per_comp` | Max reaction attempts per composition | `None` (unlimited) |
| `max_products_per_comp` | Max products per composition | `None` (unlimited) |
| `max_total_products` | Stop after this many total products | `None` (unlimited) |
| | | |
| **MoleculeHEALER / FragmentHEALER** | | |
| `sim_threshold` | Minimum Tanimoto similarity for BB matching | `0.5` |
| `max_bbs_per_frag` | Max BBs per fragment (`-1` = use threshold) | `-1` |
| `n_compositions` | Number of fragment compositions to explore | `10` |
| `retro_tree_depth` | Depth of retrosynthetic tree search | `1` |
| `min_frag_size` | Minimum fragment size in heavy atoms | `3` |
| `randomize_compositions` | Shuffle composition order | `False` |
| `random_seed` | Seed for reproducibility (`-1` = random) | `-1` |
| `custom_split_sites` | Manual bond indices to break (skips retro) | `None` |
| | | |
| **SiteHEALER** | | |
| `reactive_sites` | Atom indices for enumeration (list of ints) | `None` (all sites) |
| `rules` | Property filters, e.g., `{'MW': (0, 500)}` | `{}` |
| `struct_rules` | Required SMARTS patterns in BBs | `[]` |

### CLI Commands

```bash
healer molecule <input> [options]   # Molecule-based enumeration
healer site <input> [options]       # Site-specific enumeration  
healer fragment <input> [options]   # Fragment-based enumeration
healer view <smiles>                # Visualize with atom indices
```

Run `healer <command> --help` for detailed options.

## Web Interface

HEALER includes a web UI for interactive use.

### Local Mode (Simple)

```bash
# Start the server (no Redis needed)
healer-ui
```

Open http://localhost:8000 in your browser.

### Server Mode (Production)

For deployments with multiple users, use Cloud Tasks and a request-driven Cloud Run worker service:

```bash
Set `HEALER_SERVER_MODE=true` and configure the Cloud Tasks environment variables.
The API queues work in Cloud Tasks, which invokes the authenticated worker service
on demand; Redis retains job state and results.
```

See [web_client/README.md](web_client/README.md) for development setup.

## Configuration

Copy `.env.example` to `.env` and customize:

```bash
cp .env.example .env
```

Key settings:
- `HEALER_SERVER_MODE` — Enable async job processing
- `HEALER_DATA_DIR` — Custom data directory
- `HEALER_LIMIT_*` — Server parameter limits

## Project Structure

```
healer/
├── healer/                 # Core package
│   ├── application/        # HEALER classes
│   ├── domain/             # Data models
│   ├── utils/              # Utilities
│   ├── web/                # FastAPI backend
│   ├── scripts/            # CLI scripts
│   └── data/               # Bundled data (reactions, test BBs)
├── web_client/             # React frontend
├── tests/                  # Test suite
└── benchmark/              # Benchmarking scripts
```

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Setup

```bash
git clone https://github.com/eneskelestemur/healer.git
cd healer
conda env create -f environment.yml
conda activate healer
pip install -e ".[web]"

# Run tests
pytest tests/

# Start frontend dev server
cd web_client && npm install && npm run dev
```

## Citation

If you use HEALER in your research, please cite:

```bibtex
@article{healer2025,
  title={HEALER: Hit Expansion to Advanced Leads Using Enumerated Reactions},
  author={Kelestemur, Enes and ...},
  journal={...},
  year={2025}
}
```

## License

This project is licensed under the MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

- Reaction template formats mainly adapted from [datamol](https://github.com/datamol-io/datamol)
- Building block preprocessing inspired by retrosynthesis literature
- [Ketcher](https://github.com/epam/ketcher) for molecular drawing

---

<p align="center">
  <a href="https://github.com/eneskelestemur/healer/issues">Report Bug</a> •
  <a href="https://github.com/eneskelestemur/healer/issues">Request Feature</a>
</p>
