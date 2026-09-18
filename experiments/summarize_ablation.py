"""Print a compact per-group + comparison summary from a benchmark_report.json.

Usage:
    python experiments/summarize_ablation.py benchmarks/real_ablation_v3_20260915
    python experiments/summarize_ablation.py benchmarks/confirmatory_pareto_v3_1_20260916
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _fmt(metric_dict, fmt="{:.3f}±{:.3f}"):
    if not metric_dict:
        return "—"
    mean = metric_dict.get("mean")
    stdev = metric_dict.get("stdev")
    if mean is None:
        return "—"
    if stdev is None:
        return f"{mean:.3f}"
    return fmt.format(mean, stdev)


def _row(label, *cells, widths):
    out = label.ljust(widths[0])
    for cell, w in zip(cells, widths[1:]):
        if isinstance(cell, (int, float)):
            out += f"{cell:>{w + 2}}"
        else:
            out += cell.rjust(w + 2)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark_dir", help="Path to a benchmark output directory")
    args = parser.parse_args()

    report_path = Path(args.benchmark_dir) / "benchmark_report.json"
    if not report_path.exists():
        print(f"ERROR: {report_path} not found", file=sys.stderr)
        return 1

    r = json.loads(report_path.read_text(encoding="utf-8"))

    print(f"benchmark_id   : {r['benchmark_id']}")
    print(f"primary_metric : {r['primary_metric']}")
    print(f"baseline_group : {r['baseline_group']}")
    print(f"groups         : {list(r['groups'].keys())}")
    print()

    print("=== Group summary ===")
    headers = ("group", "n", "composite", "vina", "safe_comp", "safe_vina",
               "hERG", "unique", "scaffold", "xrd_dup", "tokens", "new_dock")
    widths = [18, 3, 16, 14, 16, 14, 9, 7, 9, 8, 9, 9]
    print(_row(*headers, widths=widths))
    for name, g in r["groups"].items():
        m = g["metrics"]
        print(_row(
            name,
            g["runs"],
            _fmt(m.get("best_composite_global"), "{:.4f}±{:.4f}"),
            _fmt(m.get("best_vina_global"), "{:.3f}±{:.3f}"),
            _fmt(m.get("best_safe_composite_global"), "{:.4f}±{:.4f}"),
            _fmt(m.get("best_safe_vina_global"), "{:.3f}±{:.3f}"),
            _fmt(m.get("mean_herg_risk_score"), "{:.3f}±{:.3f}"),
            f"{m['unique_valid_smiles']['mean']:.1f}",
            f"{m['unique_scaffolds']['mean']:.1f}",
            f"{m['cross_round_duplicates']['mean']:.1f}",
            f"{m['tokens_used']['mean']:.0f}",
            f"{m['new_dockings']['mean']:.1f}",
            widths=widths,
        ))
    print()

    print(f"=== Comparisons vs {r['baseline_group']} ({r['primary_metric']}) ===")
    for name, comp in r["comparisons"].items():
        primary = comp[r["primary_metric"]]
        print(f"  {name}: delta={primary['delta_vs_baseline']}, p={primary['welch_p_value']}, "
              f"favorable={primary['favorable']}, sig={primary['statistically_significant']}")
    print()

    if r.get("confirmatory_decision"):
        print("=== Confirmatory decision ===")
        d = r["confirmatory_decision"]
        print(f"  status              : {d['status']}")
        print(f"  reference_group     : {d['reference_group']}")
        print(f"  treatment_group     : {d['treatment_group']}")
        print(f"  efficacy_supported  : {d['efficacy_supported']}")
        print(f"  stable_improvement  : {d['stable_improvement']}")
        print(f"  safety_noninferior  : {d['safety_noninferior']}")
        print(f"  primary CI95        : {d['primary_result']['delta_ci95']}")
        print(f"  safety CI95         : {d['safety_delta_ci95']}")
        print()

    if r.get("incremental_comparisons"):
        print("=== Incremental: reflection_memory vs reflection ===")
        inc = r["incremental_comparisons"]["reflection_memory_vs_reflection"]
        for metric in (
            "best_composite_global",
            "best_vina_global",
            "best_safe_composite_global",
            "best_safe_vina_global",
            "cross_round_duplicates",
            "tokens_used",
            "best_vina_delta",
            "mean_herg_risk_score",
        ):
            if metric in inc:
                info = inc[metric]
                print(f"  {metric:<32s} delta={info['delta_vs_reference']!s:>10s} "
                      f"p={info['welch_p_value']!s:>10s} fav={info['favorable']}")
        print()

    print(f"excluded_runs : {len(r.get('excluded_runs', []))}")
    print(f"interpretation: {r['interpretation']['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())