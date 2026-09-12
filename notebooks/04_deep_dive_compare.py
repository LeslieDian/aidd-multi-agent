"""notebooks/04_deep_dive_compare.py - Phase C: Vina precision comparison.

Visualizes the deep_dive_vina.py results:
- Bar chart: best Vina per molecule (best of 6 settings)
- Bars colored by category (inhibitor / candidate / decoy)
"""
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt

import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="docs/figures/vina_deep_dive.json")
ap.add_argument("--out", default="docs/figures/04_deep_dive_compare.png")
args = ap.parse_args()

data = json.loads(Path(args.data).read_text(encoding="utf-8"))

# Group: take best (most negative) score per molecule
by_mol: dict[str, dict] = {}
for r in data:
    if not r["valid"]:
        continue
    label = r["label"]
    prev = by_mol.get(label)
    if prev is None or r["score"] < prev["score"]:
        by_mol[label] = r

# Order: positive controls, our candidates, decoys
positive = ["erlotinib", "gefitinib", "afatinib"]
candidates = [k for k in by_mol if k.startswith("p3_")]
decoys = ["aspirin", "ibuprofen", "ethanol"]

ordered = positive + candidates + decoys
scores = [by_mol[k]["score"] for k in ordered if k in by_mol]

# Color map
color_map = {"EGFR_inhibitor": "#2E86AB", "candidate": "#F18F01", "decoy": "#A23B72"}
colors = []
for k in ordered:
    if k not in by_mol:
        continue
    colors.append(color_map.get(by_mol[k]["category"], "#888"))

# Plot
fig, ax = plt.subplots(figsize=(9, 5.5))
x_pos = list(range(len(ordered)))
bars = ax.bar([x for x, k in zip(x_pos, ordered) if k in by_mol],
              scores, color=colors, edgecolor="black", linewidth=0.5)

# Highlight candidate section
for i, k in enumerate(ordered):
    if k in by_mol and k.startswith("p3_"):
        bars[i].set_edgecolor("red")
        bars[i].set_linewidth(2)

# Labels
ax.set_xticks(x_pos)
ax.set_xticklabels(ordered, rotation=30, ha="right", fontsize=10)
ax.set_ylabel("Best Vina score (kcal/mol, more negative = better)", fontsize=11)
ax.set_title("Phase C: Vina Deep-Dive at exhaustiveness 8/32/64", fontsize=13)
ax.axhline(0, color="black", linewidth=0.5)
ax.grid(axis="y", alpha=0.3)

# Annotate values
for bar, score in zip(bars, scores):
    ax.text(bar.get_x() + bar.get_width() / 2, score - 0.15,
            f"{score:.2f}", ha="center", va="top", fontsize=9, color="white",
            fontweight="bold")

# Legend
from matplotlib.patches import Patch
legend_elems = [
    Patch(facecolor="#2E86AB", label="Known EGFR inhibitor (control)"),
    Patch(facecolor="#F18F01", label="Our Phase 3 candidate"),
    Patch(facecolor="#A23B72", label="Decoy (negative control)"),
]
ax.legend(handles=legend_elems, loc="upper left")

Path(args.out).parent.mkdir(parents=True, exist_ok=True)
plt.tight_layout()
plt.savefig(args.out, dpi=150)
print(f"[OK] Saved {args.out}")

# Convergence: show spread (max - min) per molecule
print()
print("Convergence (max-min spread across settings):")
for k in ordered:
    if k not in by_mol:
        continue
    s = [r["score"] for r in data if r["valid"] and r["label"] == k]
    spread = max(s) - min(s)
    print(f"  {k:<12} spread={spread:.2f}  (converged if <0.5)")