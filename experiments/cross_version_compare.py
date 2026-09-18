"""Cross-version A/B/C comparison (works on benchmarks without benchmark_report.json).

For older benchmarks that pre-date reporting.py, we rebuild summary stats directly
from summary.json + round_*.json using the same logic.

Usage:
    python experiments/cross_version_compare.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.reporting import extract_run_metrics  # noqa: E402


def _safe(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return v
    return None


def _stats(values: list[float]) -> dict:
    finite = [v for v in values if _safe(v) is not None]
    if not finite:
        return {"n": 0, "mean": None, "stdev": None}
    return {
        "n": len(finite),
        "mean": round(statistics.mean(finite), 6),
        "stdev": round(statistics.stdev(finite), 6) if len(finite) >= 2 else 0.0,
    }


def collect(benchmark_dir: Path) -> dict:
    runs = []
    for group_dir in sorted([p for p in benchmark_dir.iterdir() if p.is_dir()]):
        nested = sorted(group_dir.glob("repeat_*/attempt_*"))
        legacy = sorted(
            [p for p in group_dir.glob("repeat_*") if (p / "summary.json").exists()]
        )
        for run_dir in nested + legacy:
            if not (run_dir / "summary.json").exists():
                continue
            try:
                m = extract_run_metrics(run_dir)
            except Exception as exc:  # noqa: BLE001
                continue
            runs.append(m)
    grouped: dict[str, list[dict]] = {}
    for r in runs:
        grouped.setdefault(r.get("group") or "?", []).append(r)

    out: dict = {"benchmark_dir": str(benchmark_dir), "groups": {}}
    for group, rows in grouped.items():
        metric_specs = (
            "best_composite_global",
            "best_vina_global",
            "valid_rate",
            "unique_valid_smiles",
            "unique_scaffolds",
            "cross_round_duplicates",
            "tokens_used",
            "new_dockings",
        )
        out["groups"][group] = {
            "runs": len(rows),
            "metrics": {
                metric: _stats([row.get(metric) for row in rows]) for metric in metric_specs
            },
        }
    return out


TARGETS = [
    "benchmarks/real_ablation_20260915",
    "benchmarks/real_ablation_v2_20260915",
    "benchmarks/real_ablation_v3_20260915",
    "benchmarks/confirmatory_pareto_v3_20260916",
    "benchmarks/confirmatory_pareto_v3_1_20260916",
]


def main() -> int:
    print(f"{'benchmark':<42s}  {'group':<20s}  {'n':>3s}  {'composite':>12s}  {'vina':>10s}  "
          f"{'unique':>7s}  {'xrd_dup':>8s}  {'tokens':>8s}  {'new_dock':>8s}")
    print("-" * 130)
    for rel in TARGETS:
        path = ROOT / rel
        if not (path / "benchmark_manifest.json").exists():
            print(f"{rel:<42s}  (no manifest)")
            continue
        try:
            rpt = collect(path)
        except Exception as exc:  # noqa: BLE001
            print(f"{rel:<42s}  ERROR: {exc}")
            continue
        for group, data in rpt["groups"].items():
            m = data["metrics"]
            comp = m["best_composite_global"]
            vina = m["best_vina_global"]
            comp_cell = f"{comp['mean']:.4f}±{comp['stdev']:.4f}" if comp["mean"] is not None else "-"
            vina_cell = f"{vina['mean']:.3f}±{vina['stdev']:.3f}" if vina["mean"] is not None else "-"
            print(
                f"{Path(rel).name:<42s}  {group:<20s}  {data['runs']:>3d}  "
                f"{comp_cell:>12s}  "
                f"{vina_cell:>10s}  "
                f"{m['unique_valid_smiles']['mean']:>7.1f}  "
                f"{m['cross_round_duplicates']['mean']:>8.1f}  "
                f"{m['tokens_used']['mean']:>8.0f}  "
                f"{m['new_dockings']['mean']:>8.1f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())