"""tools/validate_mol.py - SMILES validity + Lipinski + SA score.

Input:  a single SMILES string, or a list of SMILES.
Output: a standardized JSON dict (or list of dicts).

Note: SA score requires sascorer.py (not in pip).
If the file is missing, the sa_score field returns None but other fields still work.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import Crippen, Descriptors, Lipinski

# Silence RDKit stderr warnings (invalid SMILES spams a lot)
RDLogger.DisableLog("rdApp.*")

# Lazy-loaded SA scorer
_sascorer = None
_sascorer_loaded = False


def _load_sascorer():
    """Lazy-load sascorer.py if present."""
    global _sascorer, _sascorer_loaded
    if _sascorer_loaded:
        return _sascorer
    _sascorer_loaded = True
    candidate = Path(__file__).parent / "sascorer.py"
    if candidate.exists():
        import importlib.util

        spec = importlib.util.spec_from_file_location("sascorer", candidate)
        if spec and spec.loader:
            _sascorer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_sascorer)
    return _sascorer


def compute_sa_score(mol: Chem.Mol) -> float | None:
    """Compute SA score (1 = easy to synthesize, 10 = nearly impossible)."""
    scorer = _load_sascorer()
    if scorer is None:
        return None
    try:
        return float(scorer.calculateScore(mol))
    except Exception:
        return None


# ----------------- Core API -----------------

def validate_smiles(smiles: str) -> dict:
    """Validate one SMILES and compute chemistry descriptors.

    Returns a dict with: valid, smiles (canonical), mw, logp, hbd, hba,
    rotatable_bonds, tpsa, rings, lipinski_pass, sa_score.
    """
    if not smiles or not isinstance(smiles, str):
        return {"valid": False, "smiles": str(smiles), "error": "empty or non-string input"}

    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return {"valid": False, "smiles": smiles, "error": "RDKit cannot parse SMILES"}

    props = {
        "valid": True,
        "smiles": Chem.MolToSmiles(mol),  # canonical
        "mw": round(Descriptors.MolWt(mol), 2),
        "logp": round(Crippen.MolLogP(mol), 2),
        "hbd": Lipinski.NumHDonors(mol),
        "hba": Lipinski.NumHAcceptors(mol),
        "rotatable_bonds": Lipinski.NumRotatableBonds(mol),
        "tpsa": round(Descriptors.TPSA(mol), 2),
        "rings": Descriptors.RingCount(mol),
    }
    props["lipinski_pass"] = lipinski_pass(props)
    props["sa_score"] = compute_sa_score(mol)
    return props


def lipinski_pass(props: dict, max_mw: float = 500, max_logp: float = 5,
                  max_hbd: int = 5, max_hba: int = 10) -> bool:
    """Lipinski rule-of-five (first four rules)."""
    if not props.get("valid"):
        return False
    return (
        props.get("mw", 0) <= max_mw
        and props.get("logp", 0) <= max_logp
        and props.get("hbd", 0) <= max_hbd
        and props.get("hba", 0) <= max_hba
    )


def validate_batch(smiles_list: Iterable[str]) -> list[dict]:
    """Validate a list of SMILES, returning results in input order."""
    return [validate_smiles(s) for s in smiles_list]


# ----------------- CLI -----------------

def _main():
    import json
    import sys

    if len(sys.argv) > 1:
        inputs = sys.argv[1:]
    else:
        inputs = ["CC(=O)Oc1ccccc1C(=O)O", "CCO", "InvalidSMILES"]

    results = validate_batch(inputs)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()