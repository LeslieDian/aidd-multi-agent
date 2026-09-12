"""validate_mol — SMILES 合法性 + Lipinski + SA score

输入：单个 SMILES 字符串，或 SMILES 列表
输出：标准化 JSON 字典列表

注意：SA score 需要 sascorer.py 文件（不在 pip 包里）。
如果没有该文件，SA score 字段返回 None，不影响其他字段。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem, Crippen, Descriptors, Lipinski

# 抑制 RDKit 的 stderr warning（无效 SMILES 会刷屏）
RDLogger.DisableLog("rdApp.*")

# 可选：SA score scorer（懒加载）
_sascorer = None
_sascorer_loaded = False


def _load_sascorer():
    """懒加载 sascorer.py（如果存在）"""
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
    """计算 SA score，1=容易合成，10=几乎不可能。失败返回 None。"""
    scorer = _load_sascorer()
    if scorer is None:
        return None
    try:
        return float(scorer.calculateScore(mol))
    except Exception:
        return None


# ----------------- 核心函数 -----------------

def validate_smiles(smiles: str) -> dict:
    """验证单个 SMILES 并计算化学描述符。

    Returns:
        dict: {
            "valid": bool,
            "smiles": canonical SMILES or original,
            "mw": float,
            "logp": float,
            "hbd": int,
            "hba": int,
            "rotatable_bonds": int,
            "tpsa": float,
            "rings": int,
            "lipinski_pass": bool,
            "sa_score": float | None,
        }
    """
    if not smiles or not isinstance(smiles, str):
        return {"valid": False, "smiles": str(smiles), "error": "empty or non-string input"}

    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return {"valid": False, "smiles": smiles, "error": "RDKit cannot parse SMILES"}

    props = {
        "valid": True,
        "smiles": Chem.MolToSmiles(mol),  # 规范化
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
    """Lipinski 五规则（仅前四项）。"""
    if not props.get("valid"):
        return False
    return (
        props.get("mw", 0) <= max_mw
        and props.get("logp", 0) <= max_logp
        and props.get("hbd", 0) <= max_hbd
        and props.get("hba", 0) <= max_hba
    )


def validate_batch(smiles_list: Iterable[str]) -> list[dict]:
    """批量验证，返回与输入等长的结果列表（保序）。"""
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