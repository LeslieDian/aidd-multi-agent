"""tests/test_tools.py - Smoke tests for the 4 tools (Phase 1).

Run: python tests/test_tools.py

Expected: all 4 tools PASS; dock_score gracefully skips if Vina missing.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `from tools import ...` from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import (
    validate_batch,
    admet_batch,
    scaffold_diversity,
    dock_batch,
    is_vina_available,
)


# ---------- Test data: 10 known drugs + deliberate bad input ----------
TEST_DRUGS = [
    "CC(=O)Oc1ccccc1C(=O)O",               # 1. aspirin
    "CC(C)Cc1ccc(C(C)C(=O)O)cc1",           # 2. ibuprofen
    "CN1CCC[C@H]1c1cccnc1",                 # 3. nicotine
    "CC1=C(C(=O)NC(=N1)N)CCCCN",            # 4. minoxidil-like
    "Cn1cnc2c1c(=O)n(C)c(=O)n2C",           # 5. caffeine
    "OC(=O)C1CCCCC1",                       # 6. cyclohexanecarboxylic acid
    "CC(=O)NCC(=O)N",                       # 7. glycinamide
    "InvalidSMILES!!!",                     # 8. deliberate bad input
    "",                                     # 9. empty
    "c1ccccc1c1ccccc1",                     # 10. biphenyl
]

VALID_IDX = [0, 1, 2, 3, 4, 5, 6, 9]  # expect 8 valid


def test_validate_mol():
    print("\n=== test_validate_mol ===")
    results = validate_batch(TEST_DRUGS)
    valid_indices = [i for i, r in enumerate(results) if r["valid"]]
    assert valid_indices == VALID_IDX, f"expected {VALID_IDX}, got {valid_indices}"

    aspirin = results[0]
    assert aspirin["valid"]
    assert 170 < aspirin["mw"] < 190, f"aspirin MW out of range: {aspirin['mw']}"
    assert aspirin["lipinski_pass"], "aspirin should pass Lipinski"

    print(f"  [OK] valid molecules: {len(valid_indices)}/{len(TEST_DRUGS)}")
    print(f"  [OK] aspirin MW={aspirin['mw']}, logP={aspirin['logp']}, Lipinski={aspirin['lipinski_pass']}")
    print(f"  [OK] SA score = {aspirin['sa_score']} (None if sascorer.py not installed)")


def test_admet():
    print("\n=== test_admet ===")
    valid = [s for s in TEST_DRUGS if s and "Invalid" not in s and s != ""]
    results = admet_batch(valid)
    assert all(r["valid"] for r in results), "all valid SMILES should pass ADMET"

    qeds = [r["qed"] for r in results]
    summaries = [r["summary_score"] for r in results]
    print(f"  [OK] QED range: {min(qeds):.3f} ~ {max(qeds):.3f}")
    print(f"  [OK] summary_score range: {min(summaries):.3f} ~ {max(summaries):.3f}")
    no_herg = [r for r in results if r["herg_risk"] == 0.0]
    assert len(no_herg) >= 1


def test_diversity():
    print("\n=== test_diversity ===")
    valid = [s for s in TEST_DRUGS if s and "Invalid" not in s and s != ""]
    div = scaffold_diversity(valid)
    print(f"  [OK] unique scaffolds: {div['n_unique_scaffolds']} / {div['n_valid_scaffolds']}")
    print(f"  [OK] scaffolds: {div['scaffolds'][:3]}")
    assert div["n_unique_scaffolds"] >= 3, "should have at least 3 unique scaffolds"


def test_dock_score():
    print("\n=== test_dock_score ===")
    if not is_vina_available():
        print("  [!] Vina not installed, skipping docking test")
        print("  [!] install via: conda install -c conda-forge vina")
        print("  [!] or place vina.exe in tools/")
        return

    results = dock_batch(
        ["CCO"],
        receptor_pdb="data/1M17.pdbqt",
        pocket_center=(22.014, 0.253, 52.794),
        pocket_size=(27.7, 16.7, 19.1),
        exhaustiveness=4,
    )
    assert results[0]["valid"], f"docking failed: {results[0]['error']}"
    assert results[0]["score"] is not None
    print(f"  [OK] ethanol docking score: {results[0]['score']:.2f} kcal/mol")


def main():
    print("[TEST] AIDD Phase 1 Tool Tests")
    print("=" * 50)
    test_validate_mol()
    test_admet()
    test_diversity()
    test_dock_score()
    print("\n" + "=" * 50)
    print("[OK] All basic tool tests passed!")


if __name__ == "__main__":
    main()