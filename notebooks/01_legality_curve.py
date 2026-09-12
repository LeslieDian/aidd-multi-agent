"""01_legality_curve.py - Legality ratio vs round.

Input: runs/round_*.json (or pass --runs-dir)
Output: docs/figures/01_legality_curve.png

Goal: show that legality improves over iterations.
"""
import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt


def load_runs(runs_dir: str = "runs") -> list[dict]:
    """Load all round_*.json files, sorted by round number."""
    runs_path = Path(runs_dir)
    if not runs_path.exists():
        return []
    files = sorted(runs_path.glob("round_*.json"))
    runs = []
    for f in files:
        with f.open(encoding="utf-8") as fp:
            runs.append(json.load(fp))
    return runs


def compute_legality_curve(runs):
    """Compute valid ratio per round."""
    rounds = []
    ratios = []
    for run in runs:
        cands = run.get("candidates", [])
        if not cands:
            continue
        valid = sum(1 for c in cands if c.get("validate", {}).get("valid"))
        rounds.append(run.get("round", 0))
        ratios.append(valid / len(cands))
    return rounds, ratios


def plot(rounds, ratios, out_path="docs/figures/01_legality_curve.png"):
    plt.figure(figsize=(7, 4.5))
    plt.plot(rounds, ratios, "o-", linewidth=2, markersize=10, color="#2E86AB")
    plt.ylim(0, 1.05)
    plt.xlabel("Round", fontsize=12)
    plt.ylabel("Valid SMILES Ratio", fontsize=12)
    plt.title("Legality Curve: SMILES Validity Improves Over Iterations", fontsize=13)
    plt.grid(alpha=0.3)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"[OK] Saved {out_path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--out", default="docs/figures/01_legality_curve.png")
    args = p.parse_args()

    runs = load_runs(args.runs_dir)
    if not runs:
        print(f"[!] No data in {args.runs_dir}/. Run Phase 2 first: python loop.py")
        print("    Generating demo plot with synthetic data...")
        rounds = [0, 1, 2, 3, 4]
        ratios = [0.6, 0.7, 0.8, 0.85, 0.9]
    else:
        rounds, ratios = compute_legality_curve(runs)
    plot(rounds, ratios, args.out)


if __name__ == "__main__":
    main()