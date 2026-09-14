"""tools/diversity.py - Bemis-Murcko scaffold diversity and Tanimoto similarity.

Core API:
- get_scaffold(smi) -> MurckoScaffold SMILES
- scaffold_diversity(list) -> unique scaffolds + counts
- batch_scaffolds(list) -> per-molecule scaffolds
- tanimoto_matrix(list) -> N x N similarity matrix
"""
from __future__ import annotations

from typing import Iterable

from rdkit import Chem, RDLogger, DataStructs
from rdkit.Chem import AllChem
from rdkit.Chem.Scaffolds import MurckoScaffold

RDLogger.DisableLog("rdApp.*")


def get_scaffold(smiles: str) -> str | None:
    """Return the Bemis-Murcko scaffold SMILES (side chains removed)."""
    if not smiles or not isinstance(smiles, str):
        return None
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return None
    try:
        scaffold = MurckoScaffold.GetScaffoldForMol(mol)
        return Chem.MolToSmiles(scaffold)
    except Exception:
        return None


def batch_scaffolds(smiles_list: Iterable[str]) -> list[str | None]:
    return [get_scaffold(s) for s in smiles_list]


def scaffold_diversity(smiles_list: Iterable[str]) -> dict:
    """Count unique scaffolds.

    Returns:
        dict: {
            "n_molecules": int,
            "n_valid_scaffolds": int,
            "n_unique_scaffolds": int,
            "scaffolds": list[str],       # sorted unique
            "diversity_ratio": float,     # unique / valid
        }
    """
    smiles_list = list(smiles_list)
    scaffolds = batch_scaffolds(smiles_list)
    valid = [s for s in scaffolds if s]
    unique = sorted(set(valid))
    return {
        "n_molecules": len(smiles_list),
        "n_valid_scaffolds": len(valid),
        "n_unique_scaffolds": len(unique),
        "scaffolds": unique,
        "diversity_ratio": round(len(unique) / max(len(valid), 1), 3),
    }


def tanimoto_matrix(smiles_list: Iterable[str]) -> dict:
    """Compute pairwise Tanimoto similarity (Morgan fingerprint, r=2, 2048 bits).

    Returns:
        dict: {
            "smiles": list[str],
            "matrix": list[list[float]],   # N x N symmetric
        }
    """
    smiles_list = list(smiles_list)
    mols = [Chem.MolFromSmiles(s) for s in smiles_list]
    fps = []
    for m in mols:
        if m is None:
            fps.append(None)
            continue
        fps.append(AllChem.GetMorganFingerprintAsBitVect(m, radius=2, nBits=2048))

    n = len(fps)
    matrix = [[1.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if fps[i] is None or fps[j] is None:
                sim = 0.0
            else:
                sim = DataStructs.TanimotoSimilarity(fps[i], fps[j])
            matrix[i][j] = round(sim, 3)
            matrix[j][i] = round(sim, 3)
    return {"smiles": smiles_list, "matrix": matrix}


def tanimoto_to_reference(smiles: str, reference: str) -> float | None:
    """Tanimoto similarity of `smiles` to a single `reference` (Morgan r=2).

    Returns:
        float in [0, 1] if both parse, else None.
    """
    if not smiles or not reference:
        return None
    ref_mol = Chem.MolFromSmiles(reference.strip())
    cand_mol = Chem.MolFromSmiles(smiles.strip())
    if ref_mol is None or cand_mol is None:
        return None
    ref_fp = AllChem.GetMorganFingerprintAsBitVect(ref_mol, radius=2, nBits=2048)
    cand_fp = AllChem.GetMorganFingerprintAsBitVect(cand_mol, radius=2, nBits=2048)
    return round(DataStructs.TanimotoSimilarity(ref_fp, cand_fp), 3)


def batch_tanimoto_to_reference(
    smiles_list: Iterable[str],
    reference: str,
) -> list[float | None]:
    """Tanimoto similarity of each candidate in `smiles_list` to one reference.

    Returns a list the same length as `smiles_list`; entries are None if
    either the candidate or the reference failed to parse.
    """
    return [tanimoto_to_reference(s, reference) for s in smiles_list]


def adoption_stats(
    smiles_list: Iterable[str],
    reference: str,
    threshold: float = 0.7,
) -> dict:
    """Compute how many of `smiles_list` 'adopt' `reference` (Tanimoto > threshold).

    Returns a dict with:
        - n_total: int
        - n_valid_sim: int (candidates with non-None similarity)
        - n_adopted: int (similarity > threshold)
        - adoption_rate: float (n_adopted / max(n_valid_sim, 1))
        - max_similarity: float | None
        - mean_similarity: float | None
        - threshold: float
    """
    sims = batch_tanimoto_to_reference(smiles_list, reference)
    valid = [s for s in sims if s is not None]
    n_adopted = sum(1 for s in valid if s > threshold)
    return {
        "n_total": len(sims),
        "n_valid_sim": len(valid),
        "n_adopted": n_adopted,
        "adoption_rate": round(n_adopted / max(len(valid), 1), 3),
        "max_similarity": round(max(valid), 3) if valid else None,
        "mean_similarity": round(sum(valid) / len(valid), 3) if valid else None,
        "threshold": threshold,
        "reference": reference,
    }


# ----------------- CLI -----------------

def _main():
    import json
    import sys

    inputs = sys.argv[1:] if len(sys.argv) > 1 else [
        "CC(=O)Oc1ccccc1C(=O)O",
        "CCO",
        "c1ccccc1c1ccccc1",
    ]
    print(json.dumps(scaffold_diversity(inputs), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()