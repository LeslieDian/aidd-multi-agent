"""admet_score — 基于 RDKit 描述符的 ADMET 近似评分

注意：这是**估算**，不是真实 ADMET 预测（没有集成 admetSAR / SwissADME）。
适合在 Agent 闭环里做**快速粗筛**——把明显不合理的分子剔掉。

真实精度更高的 ADMET 可选：
- padelpy（调用 PaDEL-Descriptor 软件）
- admetSAR 2.0 web API
- 自训 QSAR 模型
"""
from __future__ import annotations

from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, Lipinski, QED

RDLogger.DisableLog("rdApp.*")


def estimate_admet(smiles: str) -> dict:
    """估算 ADMET 多目标评分。

    Returns:
        dict: {
            "valid": bool,
            "smiles": str,
            "absorption": float,        # 1.0 完美，越低越差（基于 TPSA）
            "bioavailability": float,   # 0.5/1.0，基于 Veber 规则
            "herg_risk": float,         # 0/1，hERG 心脏毒性风险
            "qed": float,               # 0-1，定量药物相似性（QED）
            "tpsa": float,
            "rotatable_bonds": int,
            "summary_score": float,     # 综合（0-1）
            "warnings": list[str],
        }
    """
    if not smiles or not isinstance(smiles, str):
        return {"valid": False, "smiles": str(smiles), "error": "empty input"}

    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return {"valid": False, "smiles": smiles, "error": "Invalid SMILES"}

    tpsa = Descriptors.TPSA(mol)
    rot_bonds = Lipinski.NumRotatableBonds(mol)
    logp = Descriptors.MolLogP(mol)
    mw = Descriptors.MolWt(mol)
    qed = QED.qed(mol)

    warnings = []

    # ---------- 1. 吸收（基于 TPSA）----------
    # 经典 Veber：TPSA ≤ 140 Å² 吸收良好
    if tpsa <= 140:
        absorption = 1.0
    elif tpsa <= 180:
        absorption = 0.5
    else:
        absorption = 0.0
        warnings.append("TPSA>180：吸收差")

    # ---------- 2. 生物利用度（Veber 规则）----------
    if rot_bonds <= 10 and tpsa <= 140:
        bioavailability = 1.0
    elif rot_bonds <= 15:
        bioavailability = 0.5
        warnings.append("rotatable_bonds 较多")
    else:
        bioavailability = 0.0
        warnings.append("rotatable_bonds>15")

    # ---------- 3. hERG 风险（粗筛：logP > 3.5 且有碱性氮）----------
    has_basic_n = any(
        atom.GetAtomicNum() == 7
        and atom.GetFormalCharge() == 0
        and atom.GetTotalNumHs() >= 1  # 有 H = 碱性
        for atom in mol.GetAtoms()
    )
    herg_risk = 1.0 if (logp > 3.5 and has_basic_n) else 0.0
    if herg_risk:
        warnings.append("hERG 风险偏高")

    # ---------- 4. 综合 ----------
    # QED 已经是 0-1 的综合药物相似度
    summary_score = (
        0.3 * absorption
        + 0.3 * bioavailability
        + 0.2 * (1 - herg_risk)
        + 0.2 * qed
    )

    return {
        "valid": True,
        "smiles": Chem.MolToSmiles(mol),
        "absorption": round(absorption, 3),
        "bioavailability": round(bioavailability, 3),
        "herg_risk": round(herg_risk, 3),
        "qed": round(qed, 3),
        "tpsa": round(tpsa, 2),
        "rotatable_bonds": rot_bonds,
        "summary_score": round(summary_score, 3),
        "warnings": warnings,
    }


def admet_batch(smiles_list: Iterable[str]) -> list[dict]:
    return [estimate_admet(s) for s in smiles_list]


# ----------------- CLI -----------------

def _main():
    import json
    import sys

    inputs = sys.argv[1:] if len(sys.argv) > 1 else ["CCO", "CC(=O)Oc1ccccc1C(=O)O"]
    print(json.dumps(admet_batch(inputs), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()