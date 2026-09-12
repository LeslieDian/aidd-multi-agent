"""dock_score — AutoDock Vina 对接打分

链路：SMILES → RDKit 3D → Meeko pdbqt → Vina → kcal/mol

依赖：
- rdkit (3D 构象生成)
- meeko (RDKit Mol → pdbqt)
- vina (Python 包 或 subprocess 调用 vina 二进制)

如果任何一环失败，函数返回 None 而不是抛异常——这样上层 Agent 可以
正常处理批次而不会因为一个坏分子崩溃。

Vina Python 包安装：conda install -c conda-forge vina
二进制安装：https://github.com/ccsb-scripps/AutoDock-Vina/releases
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

# ---------- Vina 可用性检测 ----------

def is_vina_available() -> bool:
    """检查是否能调用 vina（Python 包优先，否则找二进制）。"""
    try:
        from vina import Vina  # noqa: F401
        return True
    except ImportError:
        pass
    return shutil.which("vina") is not None


def _smiles_to_3d_mol(smiles: str):
    """SMILES → 3D RDKit mol（ETKDG + MMFF 优化）。失败返回 None。"""
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return None
    mol = Chem.AddHs(mol)
    if AllChem.EmbedMolecule(mol, AllChem.ETKDGv3()) != 0:
        return None
    AllChem.MMFFOptimizeMolecule(mol, maxIters=200)
    return mol


def _mol_to_pdbqt_string(mol) -> str | None:
    """3D mol → pdbqt string（用 Meeko）。"""
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
    """把受体 PDB 转为 pdbqt（用 ADFRsuite 的 prepare_receptor）。

    如果工具不可用，尝试用 Vina 的简化模式（直接读 PDB）。
    返回 pdbqt 文件内容字符串。
    """
    receptor_pdb = Path(receptor_pdb)
    if not receptor_pdb.exists():
        return None
    # 简化做法：直接把 PDB 内容当 pdbqt 喂给 Vina（部分 Vina 版本支持）
    return receptor_pdb.read_text()


# ---------- 主函数 ----------

def dock_smiles(
    smiles: str,
    receptor_pdb: str | Path,
    pocket_center: tuple[float, float, float],
    pocket_size: tuple[float, float, float] = (22.0, 22.0, 22.0),
    vina_binary: str = "vina",
    exhaustiveness: int = 8,
    n_poses: int = 5,
) -> dict:
    """单个分子对接。

    Returns:
        dict: {
            "smiles": str,
            "score": float | None,    # kcal/mol，越负越好
            "valid": bool,
            "error": str | None,
        }
    """
    smiles = (smiles or "").strip()
    if not smiles:
        return {"smiles": smiles, "score": None, "valid": False, "error": "empty input"}

    # 1) SMILES → 3D
    mol = _smiles_to_3d_mol(smiles)
    if mol is None:
        return {"smiles": smiles, "score": None, "valid": False, "error": "3D embedding failed"}

    # 2) mol → pdbqt
    ligand_pdbqt = _mol_to_pdbqt_string(mol)
    if ligand_pdbqt is None:
        return {"smiles": smiles, "score": None, "valid": False, "error": "Meeko conversion failed"}

    # 3) 调用 Vina
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lig_path = tmp / "lig.pdbqt"
        lig_path.write_text(ligand_pdbqt)

        # 受体直接用 pdb（让 Vina 自己解析）
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
        except FileNotFoundError:
            return {"smiles": smiles, "score": None, "valid": False,
                    "error": "vina binary not found"}
        except subprocess.TimeoutExpired:
            return {"smiles": smiles, "score": None, "valid": False,
                    "error": "vina timeout (>120s)"}

        # 解析输出：第一条 "VINA RESULT: ..." 即 best score
        for line in result.stdout.splitlines():
            if line.startswith("VINA RESULT:"):
                try:
                    score = float(line.split()[2])
                    return {"smiles": smiles, "score": score, "valid": True, "error": None}
                except (IndexError, ValueError):
                    continue

        return {"smiles": smiles, "score": None, "valid": False,
                "error": "could not parse vina output"}


def dock_batch(
    smiles_list: Iterable[str],
    receptor_pdb: str | Path,
    pocket_center: tuple[float, float, float],
    pocket_size: tuple[float, float, float] = (22.0, 22.0, 22.0),
    vina_binary: str = "vina",
    **kwargs,
) -> list[dict]:
    """批量对接，逐个返回结果。"""
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
            "hint": "conda install -c conda-forge vina",
        }, indent=2))
        return

    smiles = sys.argv[1] if len(sys.argv) > 1 else "CCO"
    receptor = sys.argv[2] if len(sys.argv) > 2 else "data/1M17.pdb"
    print(json.dumps(dock_smiles(smiles, receptor, (11, 17, 28)), indent=2))


if __name__ == "__main__":
    _main()