"""scripts/deep_dive_vina.py - High-precision Vina docking.

Goal: verify whether our generated molecules have REAL drug-like binding
affinity, not just artifacts of low-precision docking.

Compares:
- 3 known EGFR inhibitors (positive controls)
- 3 decoys (negative controls)
- 5 best molecules from our Phase 3 run (real candidates)

Each docked at:
- exhaustiveness = 8, 32, 64
- n_poses = 5, 20

Total = 9 molecules x 3 exhaustiveness x 2 n_poses = 54 runs.
At ~30s each, that's ~25 minutes total.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools import dock_smiles

RECEPTOR = "data/1M17.pdbqt"
CENTER = (22.014, 0.253, 52.794)
SIZE = (27.7, 16.7, 19.1)

# (label, SMILES, category)
PANEL = [
    # EGFR positive controls
    ("erlotinib",  "C#Cc1ccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)cc1", "EGFR inhibitor"),
    ("gefitinib",  "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1", "EGFR inhibitor"),
    ("afatinib",   "CN(C)C(=O)C1=CC=CC=C1C(=O)Nc1ncnc2cc(NCc3ccc(C)cc3)c(OC)cc12", "EGFR inhibitor"),
    # Decoys (should NOT bind)
    ("aspirin",    "CC(=O)Oc1ccccc1C(=O)O", "decoy"),
    ("ibuprofen",  "CC(C)Cc1ccc(C(C)C(=O)O)cc1", "decoy"),
    ("ethanol",    "CCO", "decoy"),
]

# Load our Phase 3 best molecules
PHASE3_BEST = []
phase3_dir = Path("runs/samples")
for p in sorted(phase3_dir.glob("round_phase3_*.json")):
    r = json.loads(p.read_text(encoding="utf-8"))
    for c in r["candidates"]:
        if c["validate"]["valid"] and c["dock"].get("score") is not None:
            PHASE3_BEST.append((c["smiles"], c["dock"]["score"], c["provider"]))

# Top 5 by phase-3 Vina score
PHASE3_BEST.sort(key=lambda x: x[1])
PHASE3_BEST = PHASE3_BEST[:5]
print(f"Loaded {len(PHASE3_BEST)} top Phase 3 molecules (best Vina in run: "
      f"{PHASE3_BEST[0][1]:.2f})")
for smi, score, prov in PHASE3_BEST:
    print(f"  {prov}: {smi} (round={score:.2f})")
print()

EXHAUSTIVENESS_LEVELS = [8, 32, 64]
N_POSES_LEVELS = [5, 20]


def main():
    out_path = Path("docs/figures/vina_deep_dive.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    results = []
    total_runs = len(PANEL) + len(PHASE3_BEST)
    total_runs *= len(EXHAUSTIVENESS_LEVELS) * len(N_POSES_LEVELS)
    print(f"Total runs: {total_runs} (~{total_runs * 30 // 60} min)")
    print()

    start = time.time()
    for label, smi, cat in PANEL:
        for phase_smi, phase_score, prov in [(None, None, None)] * 0:  # placeholder
            pass

    # Build combined list
    all_jobs = []
    for label, smi, cat in PANEL:
        all_jobs.append((label, smi, cat, None))
    for i, (smi, score, prov) in enumerate(PHASE3_BEST):
        all_jobs.append((f"phase3_top{i}", smi, "candidate", prov))

    for label, smi, cat, prov in all_jobs:
        for exh in EXHAUSTIVENESS_LEVELS:
            for n_poses in N_POSES_LEVELS:
                t0 = time.time()
                r = dock_smiles(
                    smi, RECEPTOR, CENTER, SIZE,
                    exhaustiveness=exh, n_poses=n_poses,
                )
                elapsed = time.time() - t0
                results.append({
                    "label": label, "category": cat, "provider": prov,
                    "smiles": smi, "exhaustiveness": exh, "n_poses": n_poses,
                    "score": r["score"],
                    "valid": r["valid"],
                    "error": r["error"],
                    "elapsed_s": round(elapsed, 1),
                })
                if r["valid"]:
                    score_str = f"{r['score']:.2f}"
                else:
                    score_str = f"FAIL({r['error'][:20] if r['error'] else '?'})"
                print(f"  [{label:<14}] exh={exh:>2} n={n_poses:>2} "
                      f"-> {score_str:>8} ({elapsed:.0f}s)")

    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print()
    print(f"Total time: {(time.time() - start) / 60:.1f} min")
    print(f"Results saved: {out_path}")

    # Quick summary
    print()
    print("=== Summary table (best score per molecule) ===")
    by_mol: dict[str, list] = {}
    for r in results:
        if r["valid"]:
            by_mol.setdefault(r["label"], []).append(r["score"])
    for label in [p[0] for p in PANEL] + [f"phase3_top{i}" for i in range(len(PHASE3_BEST))]:
        scores = by_mol.get(label, [])
        if scores:
            print(f"  {label:<14} min={min(scores):>7.2f}  "
                  f"max={max(scores):>7.2f}  spread={max(scores) - min(scores):>5.2f}  "
                  f"n={len(scores)}")


if __name__ == "__main__":
    main()