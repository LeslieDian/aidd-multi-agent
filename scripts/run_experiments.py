"""scripts/run_experiments.py - One-shot runner for the three experiment plots.

Usage: python scripts/run_experiments.py [--runs-dir DIR]

Prerequisite: completed Phase 2/3 loop runs (data in runs/ by default).
No data -> synthetic demo plots are generated.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs-dir", default="runs",
                   help="directory containing round_*.json (default: runs)")
    args = p.parse_args()

    print("[EXP] Running AIDD experiment plots...")
    scripts = [
        "notebooks/01_legality_curve.py",
        "notebooks/02_score_curve.py",
        "notebooks/03_diversity_compare.py",
    ]
    for script in scripts:
        path = ROOT / script
        if not path.exists():
            print(f"  [X] Missing: {script}")
            continue
        print(f"\n> {script} (--runs-dir {args.runs_dir})")
        result = subprocess.run(
            [sys.executable, str(path), "--runs-dir", args.runs_dir],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(f"  [!] exit {result.returncode}: {result.stderr}")

    print("\n[OK] Figures written to docs/figures/")
    print("  01_legality_curve.png")
    print("  02_score_curve.png")
    print("  03_diversity_compare.png")


if __name__ == "__main__":
    main()