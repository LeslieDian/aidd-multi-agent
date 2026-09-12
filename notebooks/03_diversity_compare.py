"""03_diversity_compare.py - Compare scaffold diversity across generator models.

Input: runs/round_*.json (or pass --runs-dir)
Output: docs/figures/03_diversity_compare.png

Goal: show that heterogeneous models generate structurally diverse molecules.
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt


def load_runs(runs_dir: str = "runs") -> list[dict]:
    p = Path(runs_dir)
    if not p.exists():
        return []
    return [json.loads(f.read_text(encoding="utf-8")) for f in
            sorted(Path(runs_dir).glob("round_*.json"))]


def collect_by_model(runs):
    """Group SMILES by source model."""
    by_model = {}
    for run in runs:
        for c in run.get("candidates", []):
            model = c.get("model", "unknown")
            by_model.setdefault(model, []).append(c.get("smiles", ""))
    return by_model


def scaffold_diversity(smiles_list):
    """Count unique Bemis-Murcko scaffolds."""
    from rdkit import Chem
    from rdkit.Chem.Scaffolds import MurckoScaffold

    scaffolds = set()
    for smi in smiles_list:
        if not smi:
            continue
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        scaf = MurckoScaffold.GetScaffoldForMol(mol)
        scaffolds.add(Chem.MolToSmiles(scaf))
    return len(scaffolds)


def plot(by_model, out_path="docs/figures/03_diversity_compare.png"):
    models = sorted(by_model.keys())
    counts = [scaffold_diversity(smis) for smis in (by_model[m] for m in models)]
    colors = ["#2E86AB", "#F18F01", "#A23B72", "#3FA796", "#C73E1D"]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    bars = ax.bar(models, counts, color=colors[:len(models)])
    ax.set_ylabel("Unique Bemis-Murcko Scaffolds", fontsize=12)
    ax.set_title("Diversity Compare: Heterogeneous Models Generate Different Scaffolds",
                 fontsize=13)
    ax.grid(axis="y", alpha=0.3)

    for bar, cnt in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                str(cnt), ha="center", fontsize=11)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"[OK] Saved {out_path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--out", default="docs/figures/03_diversity_compare.png")
    args = p.parse_args()

    runs = load_runs(args.runs_dir)
    if not runs:
        print(f"[!] No data in {args.runs_dir}/. Demo plot with synthetic data:")
        by_model = {
            "A1 (DeepSeek)": ["CCO", "CC(=O)O", "c1ccccc1"],
            "A2 (Qwen)":     ["CCN(CC)CC", "C1CCCCC1", "c1ccc2ccccc2c1"],
        }
    else:
        by_model = collect_by_model(runs)
    plot(by_model, args.out)


if __name__ == "__main__":
    main()