"""scripts/benchmark_vina.py - Sanity-check the Vina setup.

Runs Vina against a panel of known ligands (positive controls = real
EGFR inhibitors) and decoys (negative controls = unrelated drugs).
A correctly-configured pocket should give:
  - negative scores for true EGFR inhibitors
  - close-to-zero scores for non-binding small molecules

Usage:
    python scripts/benchmark_vina.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import dock_batch

# Updated to match config.yaml (erlotinib geometric center in 1M17, +6 A padding)
CENTER = (22.014, 0.253, 52.794)
SIZE = (27.7, 16.7, 19.1)
RECEPTOR = "data/1M17.pdbqt"

PANEL = [
    # (name, SMILES, expected_category)
    ("erlotinib",   "C#Cc1ccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)cc1",  "EGFR inhibitor"),
    ("gefitinib",   "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1", "EGFR inhibitor"),
    ("afatinib",    "CN(C)C(=O)C1=CC=CC=C1C(=O)Nc1ncnc2cc(NCc3ccc(C)cc3)c(OC)cc12", "EGFR inhibitor"),
    ("ibuprofen",   "CC(C)Cc1ccc(C(C)C(=O)O)cc1", "decoy (anti-inflammatory)"),
    ("aspirin",     "CC(=O)Oc1ccccc1C(=O)O", "decoy (analgesic)"),
    ("ethanol",     "CCO", "decoy (too small)"),
    ("caffeine",    "Cn1cnc2c1c(=O)n(C)c(=O)n2C", "decoy (CNS stimulant)"),
]


def main():
    print(f"Receptor:   {RECEPTOR}")
    print(f"Box center: ({CENTER[0]}, {CENTER[1]}, {CENTER[2]})")
    print(f"Box size:   ({SIZE[0]:.1f}, {SIZE[1]:.1f}, {SIZE[2]:.1f})")
    print()

    results = dock_batch([s for _, s, _ in PANEL], RECEPTOR, CENTER, SIZE,
                         exhaustiveness=8)

    print(f"{'Ligand':<12} {'Category':<28} {'Vina (kcal/mol)':>15}")
    print("-" * 58)
    for (name, _, cat), r in zip(PANEL, results):
        if r["valid"] and r["score"] is not None:
            print(f"{name:<12} {cat:<28} {r['score']:>15.2f}")
        else:
            err = r.get("error", "")
            print(f"{name:<12} {cat:<28} {'FAIL':>15}  {str(err)[:40]}")


if __name__ == "__main__":
    main()