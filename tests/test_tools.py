"""tests/test_tools.py 鈥?4 涓伐鍏风殑鏈€灏忛獙璇?

杩愯锛歱ython tests/test_tools.py

棰勬湡锛? 涓伐鍏峰叏閮?PASS锛宒ock_score 鍦ㄦ病鏈?Vina 鏃剁粰鍑哄弸濂介檷绾с€?
"""
from __future__ import annotations

import sys
from pathlib import Path

# 璁?`from tools import ...` 鑳借窇锛坱ests/ 涓?tools/ 鍚岀骇锛?
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import (
    validate_batch,
    admet_batch,
    scaffold_diversity,
    dock_batch,
    is_vina_available,
)


# ---------- 娴嬭瘯鏁版嵁锛?0 鏉″凡鐭ヨ嵂鐗?+ 鏁呮剰閿欑殑 ----------
TEST_DRUGS = [
    "CC(=O)Oc1ccccc1C(=O)O",               # 1. 闃垮徃鍖规灄
    "CC(C)Cc1ccc(C(C)C(=O)O)cc1",           # 2. 甯冩礇鑺?
    "CN1CCC[C@H]1c1cccnc1",                 # 3. 灏煎彜涓?
    "CC1=C(C(=O)NC(=N1)N)CCCCN",            # 4. 鍢у暥绫伙紙宸辩兎闆岄厷缁撴瀯鏀归€狅級
    "Cn1cnc2c1c(=O)n(C)c(=O)n2C",           # 5. 鍜栧暋鍥?
    "OC(=O)C1CCCCC1",                       # 6. 鐜繁鐢查吀
    "CC(=O)NCC(=O)N",                       # 7. 鐢樻皑閰镐箼閰?
    "InvalidSMILES!!!",                     # 8. 鏁呮剰閿?
    "",                                     # 9. 绌哄瓧绗︿覆
    "c1ccccc1c1ccccc1",                     # 10. 鑱旇嫰
]

VALID_IDX = [0, 1, 2, 3, 4, 5, 6, 9]  # 棰勬湡 8 鏉″悎娉?


def test_validate_mol():
    print("\n=== test_validate_mol ===")
    results = validate_batch(TEST_DRUGS)
    valid_indices = [i for i, r in enumerate(results) if r["valid"]]
    assert valid_indices == VALID_IDX, f"expected {VALID_IDX}, got {valid_indices}"

    # 妫€鏌ュ叧閿瓧娈?
    aspirin = results[0]
    assert aspirin["valid"]
    assert 170 < aspirin["mw"] < 190, f"aspirin MW out of range: {aspirin['mw']}"
    assert aspirin["lipinski_pass"], "aspirin should pass Lipinski"

    print(f"  鉁?鍚堟硶鍒嗗瓙鏁? {len(valid_indices)}/{len(TEST_DRUGS)}")
    print(f"  鉁?aspirin MW={aspirin['mw']}, logP={aspirin['logp']}, Lipinski={aspirin['lipinski_pass']}")
    print(f"  鉁?SA score = {aspirin['sa_score']} (None 琛ㄧず sascorer.py 鏈畨瑁咃紝涓嶅奖鍝嶉€氳繃)")


def test_admet():
    print("\n=== test_admet ===")
    valid = [s for s in TEST_DRUGS if s and "Invalid" not in s and s != ""]
    results = admet_batch(valid)
    assert all(r["valid"] for r in results), "all valid SMILES should pass ADMET"

    qeds = [r["qed"] for r in results]
    summaries = [r["summary_score"] for r in results]
    print(f"  鉁?QED 鑼冨洿: {min(qeds):.3f} ~ {max(qeds):.3f}")
    print(f"  鉁?summary_score 鑼冨洿: {min(summaries):.3f} ~ {max(summaries):.3f}")
    # 鑷冲皯鏈変竴涓垎瀛愭病鏈?hERG 璀﹀憡
    no_herg = [r for r in results if r["herg_risk"] == 0.0]
    assert len(no_herg) >= 1


def test_diversity():
    print("\n=== test_diversity ===")
    valid = [s for s in TEST_DRUGS if s and "Invalid" not in s and s != ""]
    div = scaffold_diversity(valid)
    print(f"  鉁?鍞竴楠ㄦ灦: {div['n_unique_scaffolds']} / {div['n_valid_scaffolds']}")
    print(f"  鉁?楠ㄦ灦鏍蜂緥: {div['scaffolds'][:3]}")
    assert div["n_unique_scaffolds"] >= 3, "should have at least 3 unique scaffolds"


def test_dock_score():
    print("\n=== test_dock_score ===")
    if not is_vina_available():
        print("  鈿?Vina 鏈畨瑁咃紝璺宠繃瀵规帴娴嬭瘯锛堣繖鏄鏈熺殑寮€鍙戠幆澧冿級")
        print("  鉁?瀹夎鍛戒护锛歝onda install -c conda-forge vina")
        print("  鉁?鎴栨斁缃?vina.exe 鍒?tools/ 鐩綍")
        return

    results = dock_batch(
        ["CCO"],
        receptor_pdb="data/1M17.pdbqt",
        pocket_center=(11.0, 17.0, 28.0),
        exhaustiveness=4,
    )
    assert results[0]["valid"], f"docking failed: {results[0]['error']}"
    assert results[0]["score"] is not None
    print(f"  鉁?ethanol score: {results[0]['score']:.2f} kcal/mol")


def main():
    print("馃И AIDD Phase 1 Tool Tests")
    print("=" * 50)
    test_validate_mol()
    test_admet()
    test_diversity()
    test_dock_score()
    print("\n" + "=" * 50)
    print("鉁?All basic tool tests passed!")


if __name__ == "__main__":
    main()
