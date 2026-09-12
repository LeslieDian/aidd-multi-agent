"""diversity — Bemis-Murcko 骨架多样性与相似度

核心：
- get_scaffold(smi) → MurckoScaffold SMILES
- scaffold_diversity(list) → 唯一骨架集合 + 数量
- batch_scaffolds(list) → 每分子的骨架列表
"""
from __future__ import annotations

from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit import DataStructs
from rdkit.Chem import AllChem

RDLogger.DisableLog("rdApp.*")


def get_scaffold(smiles: str) -> str | None:
    """返回 Bemis-Murcko 骨架 SMILES（去掉侧链）。失败返回 None。"""
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
    """统计唯一骨架数。

    Returns:
        dict: {
            "n_molecules": int,
            "n_valid_scaffolds": int,
            "n_unique_scaffolds": int,
            "scaffolds": list[str],          # 排序后的唯一骨架
            "diversity_ratio": float,        # unique/total
        }
    """
    scaffolds = batch_scaffolds(smiles_list)
    valid = [s for s in scaffolds if s]
    unique = sorted(set(valid))
    return {
        "n_molecules": len(list(smiles_list) if not isinstance(smiles_list, list) else smiles_list),
        "n_valid_scaffolds": len(valid),
        "n_unique_scaffolds": len(unique),
        "scaffolds": unique,
        "diversity_ratio": round(len(unique) / max(len(valid), 1), 3),
    }


def tanimoto_matrix(smiles_list: Iterable[str]) -> dict:
    """计算分子间的 Tanimoto 相似度（基于 Morgan fingerprint）。

    Returns:
        dict: {
            "smiles": list[str],
            "matrix": list[list[float]],   # N×N 对称矩阵
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