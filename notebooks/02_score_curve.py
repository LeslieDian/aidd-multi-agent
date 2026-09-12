"""02_score_curve.py - Average score (ADMET + Vina) vs round.

Input: runs/round_*.json
Output: docs/figures/02_score_curve.png

Goal: show that molecules improve on both ADMET and docking across iterations.
"""
import json
from pathlib import Path

import matplotlib.pyplot as plt


def load_runs(runs_dir: str = "runs") -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in
            sorted(Path(runs_dir).glob("round_*.json"))]


def compute_score_curve(runs):
    rounds, admet_scores, vina_scores = [], [], []
    for run in runs:
        cands = run.get("candidates", [])
        if not cands:
            continue
        admet = [c["admet"]["summary_score"] for c in cands
                 if c.get("admet", {}).get("valid")]
        vina = [c["dock"]["score"] for c in cands
                if c.get("dock", {}).get("valid") and c["dock"].get("score") is not None]
        if admet or vina:
            rounds.append(run.get("round", 0))
            admet_scores.append(sum(admet) / len(admet) if admet else float("nan"))
            vina_scores.append(sum(vina) / len(vina) if vina else float("nan"))
    return rounds, admet_scores, vina_scores


def plot(rounds, admet_scores, vina_scores,
         out_path="docs/figures/02_score_curve.png"):
    fig, ax1 = plt.subplots(figsize=(7, 4.5))

    ax1.set_xlabel("Round", fontsize=12)
    ax1.set_ylabel("Avg ADMET Score (0-1)", fontsize=12, color="#A23B72")
    ax1.plot(rounds, admet_scores, "o-", color="#A23B72", linewidth=2,
             markersize=10, label="ADMET score")
    ax1.tick_params(axis="y", labelcolor="#A23B72")
    ax1.set_ylim(0, 1.05)
    ax1.grid(alpha=0.3)

    ax2 = ax1.twinx()
    ax2.set_ylabel("Avg Vina Score (kcal/mol)", fontsize=12, color="#F18F01")
    ax2.plot(rounds, vina_scores, "s-", color="#F18F01", linewidth=2,
             markersize=10, label="Vina score")
    ax2.tick_params(axis="y", labelcolor="#F18F01")

    plt.title("Score Curves: ADMET up and Vina down over iterations", fontsize=13)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"[OK] Saved {out_path}")


def main():
    runs = load_runs()
    if not runs:
        print("[!] No runs/ data yet. Demo plot with synthetic data:")
        rounds = [0, 1, 2, 3, 4]
        admet = [0.55, 0.62, 0.71, 0.78, 0.84]
        vina = [-6.2, -6.8, -7.4, -8.1, -8.7]
    else:
        rounds, admet, vina = compute_score_curve(runs)
    plot(rounds, admet, vina)


if __name__ == "__main__":
    main()