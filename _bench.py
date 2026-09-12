"""Benchmark Vina with corrected pocket center."""
from tools import dock_batch

LIGANDS = [
    ("erlotinib", "C#Cc1ccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)cc1"),
    ("gefitinib", "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1"),
    ("afatinib", "CN(C)C(=O)C1=CC=CC=C1C(=O)Nc1ncnc2cc(NCc3ccc(C)cc3)c(OC)cc12"),
    ("ibuprofen", "CC(C)Cc1ccc(C(C)C(=O)O)cc1"),
    ("ethanol", "CCO"),
]

CENTER = (22.014, 0.253, 52.794)
SIZE = (31.709, 20.684, 23.099)
RECEPTOR = "data/1M17.pdbqt"

results = dock_batch([s for _, s in LIGANDS], RECEPTOR, CENTER, SIZE, exhaustiveness=8)

print("Ligand       Vina score")
print("-" * 26)
for (name, _), r in zip(LIGANDS, results):
    if r["valid"] and r["score"] is not None:
        print(f"{name:<12} {r['score']:>12.2f}")
    else:
        err = r.get("error", "")
        print(f"{name:<12} FAIL: {str(err)[:50]}")