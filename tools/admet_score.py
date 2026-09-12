"""admet_score 鈥?鍩轰簬 RDKit 鎻忚堪绗︾殑 ADMET 杩戜技璇勫垎

娉ㄦ剰锛氳繖鏄?*浼扮畻**锛屼笉鏄湡瀹?ADMET 棰勬祴锛堟病鏈夐泦鎴?admetSAR / SwissADME锛夈€?
閫傚悎鍦?Agent 闂幆閲屽仛**蹇€熺矖绛?*鈥斺€旀妸鏄庢樉涓嶅悎鐞嗙殑鍒嗗瓙鍓旀帀銆?

鐪熷疄绮惧害鏇撮珮鐨?ADMET 鍙€夛細
- padelpy锛堣皟鐢?PaDEL-Descriptor 杞欢锛?
- admetSAR 2.0 web API
- 鑷 QSAR 妯″瀷
"""
from __future__ import annotations

from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, Lipinski, QED

RDLogger.DisableLog("rdApp.*")


def estimate_admet(smiles: str) -> dict:
    """浼扮畻 ADMET 澶氱洰鏍囪瘎鍒嗐€?

    Returns:
        dict: {
            "valid": bool,
            "smiles": str,
            "absorption": float,        # 1.0 瀹岀編锛岃秺浣庤秺宸紙鍩轰簬 TPSA锛?
            "bioavailability": float,   # 0.5/1.0锛屽熀浜?Veber 瑙勫垯
            "herg_risk": float,         # 0/1锛宧ERG 蹇冭剰姣掓€ч闄?
            "qed": float,               # 0-1锛屽畾閲忚嵂鐗╃浉浼兼€э紙QED锛?
            "tpsa": float,
            "rotatable_bonds": int,
            "summary_score": float,     # 缁煎悎锛?-1锛?
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

    # ---------- 1. 鍚告敹锛堝熀浜?TPSA锛?---------
    # 缁忓吀 Veber锛歍PSA 鈮?140 脜虏 鍚告敹鑹ソ
    if tpsa <= 140:
        absorption = 1.0
    elif tpsa <= 180:
        absorption = 0.5
    else:
        absorption = 0.0
        warnings.append("TPSA>180锛氬惛鏀跺樊")

    # ---------- 2. 鐢熺墿鍒╃敤搴︼紙Veber 瑙勫垯锛?---------
    if rot_bonds <= 10 and tpsa <= 140:
        bioavailability = 1.0
    elif rot_bonds <= 15:
        bioavailability = 0.5
        warnings.append("rotatable_bonds 杈冨")
    else:
        bioavailability = 0.0
        warnings.append("rotatable_bonds>15")

    # ---------- 3. hERG 椋庨櫓锛堢矖绛涳細logP > 3.5 涓旀湁纰辨€ф爱锛?---------
    has_basic_n = any(
        atom.GetAtomicNum() == 7
        and atom.GetFormalCharge() == 0
        and atom.GetTotalNumHs() >= 1  # 鏈?H = 纰辨€?
        for atom in mol.GetAtoms()
    )
    herg_risk = 1.0 if (logp > 3.5 and has_basic_n) else 0.0
    if herg_risk:
        warnings.append("hERG 椋庨櫓鍋忛珮")

    # ---------- 4. 缁煎悎 ----------
    # QED 宸茬粡鏄?0-1 鐨勭患鍚堣嵂鐗╃浉浼煎害
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
