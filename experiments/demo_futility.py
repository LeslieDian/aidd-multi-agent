"""Demonstrate the confirmatory futility rule on existing v3.1 data.

The rule in scripts/run_benchmark.py::assess_confirmatory_futility is:

  minimum_successes = ceil(min_rate * planned - 1e-12)
  maximum_possible  = current_successes + (planned - completed)
  stop iff completed >= min_completed AND maximum_possible < minimum_successes

Applied to benchmarks/confirmatory_pareto_v3_1_20260916:
  - planned=10, min_improved_run_rate=0.70 -> minimum_successes=7
  - reflection_memory actual improved_run_rate = 0.1 (1/10)
  - The rule must reach "mathematically unreachable" only AFTER enough runs
    are observed; we replay the manifest run-by-run to show the threshold.

Usage:
    python experiments/demo_futility.py [benchmarks/confirmatory_pareto_v3_1_20260916]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_benchmark import assess_confirmatory_futility  # noqa: E402


def replay(manifest: dict) -> None:
    spec = manifest.get("confirmatory") or {}
    if not spec:
        print("Manifest has no confirmatory spec; skip demo.")
        return

    # If the manifest does not have its own futility rule, borrow the v4 rule.
    futility = spec.get("futility") or {
        "enabled": True,
        "min_completed": 3,
        "rule": "stop_when_minimum_improvement_rate_is_mathematically_unreachable",
    }
    spec = {**spec, "futility": futility}

    planned = int(manifest["execution"]["repeats"])
    minimum_rate = float(spec.get("min_improved_run_rate", 0.7))
    min_completed = max(1, int(futility.get("min_completed", 3)))
    minimum_successes = math.ceil(minimum_rate * planned - 1e-12)
    treatment = spec.get("treatment_group") or (
        spec.get("treatment_groups") or ["reflection_memory"]
    )[0]

    note = (
        " (borrowed from confirmatory_matrix_v4.yaml — "
        "original v3.1 manifest did not declare a futility rule)"
        if "futility" not in manifest.get("confirmatory", {})
        else ""
    )

    print(f"Planned runs        : {planned}")
    print(f"Treatment group     : {treatment}")
    print(f"Min improved rate   : {minimum_rate:.2f} -> minimum_successes = {minimum_successes}")
    print(f"min_completed gate  : {min_completed}")
    if note:
        print(f"Note{note}")
    print()
    print("  step | completed | successes | max_possible | stop?")
    print("  -----+-----------+-----------+--------------+-------")

    replayed = dict(manifest)
    replayed["confirmatory"] = spec
    runs = [r for r in manifest.get("runs", []) if r.get("group") == treatment]
    completed = 0
    successes = 0
    for run in runs:
        if not run.get("eligible"):
            continue
        summary_path = Path(run["run_dir"]) / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        improved = (summary.get("agent_metrics") or {}).get("run_shows_improvement")
        completed += 1
        if isinstance(improved, bool):
            successes += int(improved)
        replayed["runs"] = [r for r in manifest.get("runs", []) if int(r.get("repeat") or 0) <= completed]
        result = assess_confirmatory_futility(replayed)
        max_possible = successes + max(0, planned - completed)
        stop = bool(result.get("stop"))
        verdict = "STOP" if stop else "continue"
        print(f"  {completed:>4d} | {completed:>9d} | {successes:>9d} | "
              f"{max_possible:>12d} | {verdict}")
        if stop:
            print(f"\n  futility trigger: {result.get('reason')}")
            print(f"  remaining planned: {planned - completed}")
            print(f"  minimum needed   : {minimum_successes}")
            print(f"  maximum possible : {max_possible}  "
                  f"(< {minimum_successes} -> cannot reach {minimum_rate:.0%} improved rate)")
            return
    print("\n  futility rule did NOT trigger in this manifest "
          "(successful rate eventually reached the threshold).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark_dir", nargs="?",
                        default="benchmarks/confirmatory_pareto_v3_1_20260916")
    args = parser.parse_args()
    manifest_path = Path(args.benchmark_dir) / "benchmark_manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: {manifest_path} not found", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replay(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())