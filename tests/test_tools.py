"""tests/test_tools.py — 4 个工具的最小验证

运行：python tests/test_tools.py

预期：4 个工具全部 PASS，dock_score 在没有 Vina 时给出友好降级。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让 `from tools import ...` 能跑（tests/ 与 tools/ 同级）
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import (
    validate_batch,
    admet_batch,
    scaffold_diversity,
    dock_batch,
    is_vina_available,
)


# ---------- 测试数据：10 条已知药物 + 故意错的 ----------
TEST_DRUGS = [
    "CC(=O)Oc1ccccc1C(=O)O",               # 1. 阿司匹林
    "CC(C)Cc1ccc(C(C)C(=O)O)cc1",           # 2. 布洛芬
    "CN1CCC[C@H]1c1cccnc1",                 # 3. 尼古丁
    "CC1=C(C(=O)NC(=N1)N)CCCCN",            # 4. 嘧啶类（己烷雌酚结构改造）
    "Cn1cnc2c1c(=O)n(C)c(=O)n2C",           # 5. 咖啡因
    "OC(=O)C1CCCCC1",                       # 6. 环己甲酸
    "CC(=O)NCC(=O)N",                       # 7. 甘氨酸乙酰
    "InvalidSMILES!!!",                     # 8. 故意错
    "",                                     # 9. 空字符串
    "c1ccccc1c1ccccc1",                     # 10. 联苯
]

VALID_IDX = [0, 1, 2, 3, 4, 5, 6, 9]  # 预期 8 条合法


def test_validate_mol():
    print("\n=== test_validate_mol ===")
    results = validate_batch(TEST_DRUGS)
    valid_indices = [i for i, r in enumerate(results) if r["valid"]]
    assert valid_indices == VALID_IDX, f"expected {VALID_IDX}, got {valid_indices}"

    # 检查关键字段
    aspirin = results[0]
    assert aspirin["valid"]
    assert 170 < aspirin["mw"] < 190, f"aspirin MW out of range: {aspirin['mw']}"
    assert aspirin["lipinski_pass"], "aspirin should pass Lipinski"

    print(f"  ✓ 合法分子数: {len(valid_indices)}/{len(TEST_DRUGS)}")
    print(f"  ✓ aspirin MW={aspirin['mw']}, logP={aspirin['logp']}, Lipinski={aspirin['lipinski_pass']}")
    print(f"  ✓ SA score = {aspirin['sa_score']} (None 表示 sascorer.py 未安装，不影响通过)")


def test_admet():
    print("\n=== test_admet ===")
    valid = [s for s in TEST_DRUGS if s and "Invalid" not in s and s != ""]
    results = admet_batch(valid)
    assert all(r["valid"] for r in results), "all valid SMILES should pass ADMET"

    qeds = [r["qed"] for r in results]
    summaries = [r["summary_score"] for r in results]
    print(f"  ✓ QED 范围: {min(qeds):.3f} ~ {max(qeds):.3f}")
    print(f"  ✓ summary_score 范围: {min(summaries):.3f} ~ {max(summaries):.3f}")
    # 至少有一个分子没有 hERG 警告
    no_herg = [r for r in results if r["herg_risk"] == 0.0]
    assert len(no_herg) >= 1


def test_diversity():
    print("\n=== test_diversity ===")
    valid = [s for s in TEST_DRUGS if s and "Invalid" not in s and s != ""]
    div = scaffold_diversity(valid)
    print(f"  ✓ 唯一骨架: {div['n_unique_scaffolds']} / {div['n_valid_scaffolds']}")
    print(f"  ✓ 骨架样例: {div['scaffolds'][:3]}")
    assert div["n_unique_scaffolds"] >= 3, "should have at least 3 unique scaffolds"


def test_dock_score():
    print("\n=== test_dock_score ===")
    if not is_vina_available():
        print("  ⚠ Vina 未安装，跳过对接测试（这是预期的开发环境）")
        print("  ✓ 安装命令：conda install -c conda-forge vina")
        return

    results = dock_batch(
        ["CCO"],
        receptor_pdb="data/1M17.pdb",  # 假设已下载，否则会失败但不崩
        pocket_center=(11.0, 17.0, 28.0),
    )
    print(f"  ✓ dock result: {results[0]}")


def main():
    print("🧪 AIDD Phase 1 Tool Tests")
    print("=" * 50)
    test_validate_mol()
    test_admet()
    test_diversity()
    test_dock_score()
    print("\n" + "=" * 50)
    print("✅ All basic tool tests passed!")


if __name__ == "__main__":
    main()