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

import json
import math
import re
import uuid
import shutil
from tools.provenance import file_hash, versions
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
    """Availability matches the actual CLI execution backend."""
    try:
        resolve_vina_binary(vina_binary)
        return True
    except FileNotFoundError:
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

def _smiles_to_3d_mol(smiles: str, seed: int = 2026):
    """SMILES -> 3D RDKit mol (ETKDG + MMFF). Returns None on failure."""
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return None
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        return None
    if not AllChem.MMFFHasAllMoleculeParams(mol):
        return None
    if AllChem.MMFFOptimizeMolecule(mol, maxIters=1000) != 0:
        return None
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


# ---------- Main API ----------

def validate_receptor(receptor):
    receptor = Path(receptor)
    audit_path = receptor.with_suffix('.audit.json')
    if not receptor.is_file() or not audit_path.is_file():
        raise ValueError('Receptor or preparation audit missing; run scripts/prepare_receptor.py')
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    if not audit.get('preparation_passed') or audit.get('receptor_sha256') != file_hash(receptor):
        raise ValueError('Receptor preparation audit failed or hash mismatch')
    return audit


def dock_smiles(smiles, receptor_pdb, pocket_center, pocket_size=(22., 22., 22.),
                vina_binary=None, exhaustiveness=8, n_poses=5, seed=2026,
                artifact_dir=None, timeout=180, cpu=2):
    """Return an auditable result; tool errors never become molecular failures."""
    run_dir = Path(artifact_dir or 'data/docking') / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    smiles = (smiles or '').strip()
    metadata = {
        'protocol': 'vina-meeko-v2', 'receptor_sha256': file_hash(receptor_pdb),
        'center': list(pocket_center), 'size': list(pocket_size),
        'exhaustiveness': exhaustiveness, 'n_poses': n_poses, 'seed': seed,
        'cpu': cpu, 'timeout_seconds': timeout, 'versions': versions(),
        'ligand_preparation': 'ETKDGv3 seeded; MMFF converged; Meeko',
        'score_interpretation': 'uncalibrated docking score, not measured affinity',
    }
    result = {'smiles': smiles, 'score': None, 'valid': False,
              'status': 'tool_error', 'error': None, 'provenance': metadata,
              'artifacts': {'directory': str(run_dir.resolve())}}
    try:
        audit = validate_receptor(receptor_pdb)
        metadata['receptor_preparation'] = audit
        binary = resolve_vina_binary(vina_binary)
        metadata['vina_binary_sha256'] = file_hash(binary)
        version = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=10)
        metadata['vina_version'] = version.stdout.strip()
        mol = _smiles_to_3d_mol(smiles, seed)
        if mol is None:
            raise ValueError('Invalid structure, embedding failure, or MMFF not converged')
        metadata['canonical_smiles'] = Chem.MolToSmiles(Chem.RemoveHs(mol))
        ligand = _mol_to_pdbqt_string(mol)
        if not ligand:
            raise ValueError('Meeko ligand preparation failed')
        lig_path, pose_path = run_dir / 'ligand.pdbqt', run_dir / 'poses.pdbqt'
        lig_path.write_text(ligand)
        writer = Chem.SDWriter(str(run_dir / 'input.sdf'))
        writer.write(mol)
        writer.close()
        metadata['ligand_pdbqt_sha256'] = file_hash(lig_path)
        cmd = [binary, '--receptor', str(Path(receptor_pdb).resolve()),
               '--ligand', str(lig_path.resolve()), '--out', str(pose_path.resolve()),
               '--exhaustiveness', str(exhaustiveness), '--num_modes', str(n_poses),
               '--seed', str(seed), '--cpu', str(cpu)]
        for axis, center, size in zip('xyz', pocket_center, pocket_size):
            cmd.extend(['--center_' + axis, str(center), '--size_' + axis, str(size)])
        metadata['command'] = cmd
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        (run_dir / 'stdout.log').write_text(proc.stdout, encoding='utf-8')
        (run_dir / 'stderr.log').write_text(proc.stderr, encoding='utf-8')
        if proc.returncode:
            raise RuntimeError(f'Vina exit {proc.returncode}; see stderr.log')
        if not pose_path.is_file():
            raise RuntimeError('Vina returned no pose file')
        match = re.search(r'^REMARK VINA RESULT:\s+([-+0-9.eE]+)', pose_path.read_text(), re.M)
        if not match or not math.isfinite(float(match.group(1))):
            raise RuntimeError('No finite first-pose Vina score')
        result.update(score=float(match.group(1)), valid=True, status='ok')
        result['artifacts']['poses'] = str(pose_path.resolve())
        metadata['poses_sha256'] = file_hash(pose_path)
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    (run_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


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
    receptor = sys.argv[2] if len(sys.argv) > 2 else "data/prepared/1M17_v1.pdbqt"
    print(json.dumps(dock_smiles(smiles, receptor, (22.014, 0.253, 52.794), (27.7, 16.7, 19.1)), indent=2))


if __name__ == "__main__":
    _main()