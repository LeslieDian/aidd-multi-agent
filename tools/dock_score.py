"""tools/dock_score.py - AutoDock Vina docking wrapper.

Pipeline: SMILES -> RDKit 3D -> Meeko pdbqt -> Vina -> kcal/mol

Dependencies:
- rdkit (3D conformer generation)
- meeko (RDKit Mol -> pdbqt)
- vina (Python pkg or subprocess calling vina binary)

If any step fails, the function returns None instead of raising.
The upper Agent layer can process batches without crashing
because of one bad molecule.

Vina Python pkg install: conda install -c conda-forge vina
Vina binary download:    https://github.com/ccsb-scripps/AutoDock-Vina/releases
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem

RDLogger.DisableLog("rdApp.*")

# ---------- Vina availability ----------

VINA_BIN_CANDIDATES = [
    "vina",                  # PATH
    "tools/vina.exe",        # bundled (Windows)
    "tools/vina",            # bundled (Linux/macOS)
    "./vina.exe",
    "./vina",
]


def is_vina_available(vina_binary: str | None = None) -> bool:
    """Check whether vina is callable.

    Priority:
    1. explicit vina_binary
    2. Python package `vina`
    3. common paths + PATH
    """
    if vina_binary:
        return Path(vina_binary).exists()

    # Try Python pkg
    try:
        from vina import Vina  # noqa: F401
        return True
    except ImportError:
        pass

    # Common paths
    for cand in VINA_BIN_CANDIDATES:
        if "/" in cand or "\\" in cand:
            if Path(cand).exists():
                return True
        elif shutil.which(cand):
            return True
    return False


def resolve_vina_binary(vina_binary: str | None = None) -> str:
    """Resolve vina executable path. Raises FileNotFoundError if missing."""
    if vina_binary:
        if Path(vina_binary).exists():
            return vina_binary
        raise FileNotFoundError(f"vina binary not found at {vina_binary}")
    for cand in VINA_BIN_CANDIDATES:
        if Path(cand).exists():
            return str(Path(cand).resolve())
    found = shutil.which("vina")
    if found:
        return found
    raise FileNotFoundError(
        "vina not found. Install via `conda install -c conda-forge vina` "
        "or place `vina.exe` in tools/ or PATH."
    )


# ---------- Helpers ----------

def _smiles_to_3d_mol(smiles: str):
    """SMILES -> 3D RDKit mol (ETKDG + MMFF). Returns None on failure."""
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return None
    mol = Chem.AddHs(mol)
    if AllChem.EmbedMolecule(mol, AllChem.ETKDGv3()) != 0:
        return None
    AllChem.MMFFOptimizeMolecule(mol, maxIters=200)
    return mol


def _mol_to_pdbqt_string(mol) -> str | None:
    """3D mol -> pdbqt string (via Meeko). Returns None on failure."""
    try:
        from meeko import MoleculePreparation, PDBQTWriterLegacy
    except ImportError:
        return None
    try:
        prep = MoleculePreparation()
        mol_setups = prep.prepare(mol)
        pdbqt_string, is_ok, _ = PDBQTWriterLegacy.write_string(mol_setups[0])
        return pdbqt_string if is_ok else None
    except Exception:
        return None


def _prepare_receptor_pdbqt(receptor_pdb: str | Path) -> str | None:
    """Convert receptor PDB to pdbqt using OpenBabel (rigid, no torsions).

    Caller is responsible for running this once before batch docking.
    """
    receptor_pdb = Path(receptor_pdb)
    if not receptor_pdb.exists():
        return None
    return receptor_pdb.read_text()


# ---------- Main API ----------

def dock_smiles(
    smiles: str,
    receptor_pdb: str | Path,
    pocket_center: tuple[float, float, float],
    pocket_size: tuple[float, float, float] = (22.0, 22.0, 22.0),
    vina_binary: str | None = None,
    exhaustiveness: int = 8,
    n_poses: int = 5,
) -> dict:
    """Dock a single molecule.

    Returns:
        dict: {
            "smiles": str,
            "score": float | None,   # kcal/mol, more negative = better
            "valid": bool,
            "error": str | None,
        }
    """
    smiles = (smiles or "").strip()
    if not smiles:
        return {"smiles": smiles, "score": None, "valid": False, "error": "empty input"}

    # Resolve vina binary
    try:
        vina_binary = resolve_vina_binary(vina_binary)
    except FileNotFoundError as e:
        return {"smiles": smiles, "score": None, "valid": False, "error": str(e)}

    # 1) SMILES -> 3D
    mol = _smiles_to_3d_mol(smiles)
    if mol is None:
        return {"smiles": smiles, "score": None, "valid": False, "error": "3D embedding failed"}

    # 2) mol -> pdbqt
    ligand_pdbqt = _mol_to_pdbqt_string(mol)
    if ligand_pdbqt is None:
        return {"smiles": smiles, "score": None, "valid": False, "error": "Meeko conversion failed"}

    # 3) Run Vina
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lig_path = tmp / "lig.pdbqt"
        lig_path.write_text(ligand_pdbqt)

        cmd = [
            vina_binary,
            "--receptor", str(receptor_pdb),
            "--ligand", str(lig_path),
            "--center_x", str(pocket_center[0]),
            "--center_y", str(pocket_center[1]),
            "--center_z", str(pocket_center[2]),
            "--size_x", str(pocket_size[0]),
            "--size_y", str(pocket_size[1]),
            "--size_z", str(pocket_size[2]),
            "--exhaustiveness", str(exhaustiveness),
            "--num_modes", str(n_poses),
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
            if result.returncode != 0:
                return {"smiles": smiles, "score": None, "valid": False,
                        "error": f"vina exit {result.returncode}: {result.stderr[:200]}"}
        except subprocess.TimeoutExpired:
            return {"smiles": smiles, "score": None, "valid": False,
                    "error": "vina timeout (>120s)"}

        # Parse Vina 1.2+ table output:
        #   mode |   affinity | dist from best mode
        #     1      -0.3273          0          0
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                try:
                    score = float(parts[1])
                    return {"smiles": smiles, "score": score, "valid": True, "error": None}
                except ValueError:
                    continue

        return {"smiles": smiles, "score": None, "valid": False,
                "error": "could not parse vina output"}


def dock_batch(
    smiles_list: Iterable[str],
    receptor_pdb: str | Path,
    pocket_center: tuple[float, float, float],
    pocket_size: tuple[float, float, float] = (22.0, 22.0, 22.0),
    vina_binary: str | None = None,
    **kwargs,
) -> list[dict]:
    """Dock a batch of molecules, returning per-molecule results."""
    return [
        dock_smiles(s, receptor_pdb, pocket_center, pocket_size, vina_binary, **kwargs)
        for s in smiles_list
    ]


# ----------------- CLI -----------------

def _main():
    import json
    import sys

    if not is_vina_available():
        print(json.dumps({
            "vina_available": False,
            "hint": "conda install -c conda-forge vina or place tools/vina.exe",
        }, indent=2))
        return

    smiles = sys.argv[1] if len(sys.argv) > 1 else "CCO"
    receptor = sys.argv[2] if len(sys.argv) > 2 else "data/1M17.pdbqt"
    print(json.dumps(dock_smiles(smiles, receptor, (11, 17, 28)), indent=2))


if __name__ == "__main__":
    _main()