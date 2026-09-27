"""scripts/audit_pool_vs_random.py — missing sanity baseline for the AIDD loop.

Answers three questions the current experiment suite cannot answer:

1. Is the multi-agent loop actually *searching*, or is it a random sampler
   that happens to be wrapped in agent scaffolding?
   -> compares the loop's real per-round best against the extreme-value curve
      of drawing the same number of molecules at random from the pool the
      generator itself produced.  If drawing at random beats the agent, the
      feedback path is not adding search signal.

2. Is the iteration moving in any consistent direction?
   -> per-run best-safe-Vina curve + paired round-0 vs round-N comparison.

3. Is there an elitist / lineage mechanism (does round N+1 build on round N)?
   -> Tanimoto between consecutive rounds' best molecules, plus scaffold
      concentration of the whole pool.

Also reports the Vina/hERG trade-off coefficient, which is what decides
whether a safety-gated objective is *sufficient* or whether the pipeline
still needs an explicit safe-only progress signal.

Usage:
    python scripts/audit_pool_vs_random.py benchmarks/<run_dir> [--json out.json]

Only reads saved run artifacts (`round_*.json`); no docking, no LLM calls.
Requires RDKit (already a project dependency).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import numpy as np
except ImportError:  # numpy is a hard project dependency; fail loudly
    raise SystemExit("numpy is required")


def load_runs(root: str) -> tuple[dict, dict]:
    """Return ({run_key: {round: [candidates]}}, {smiles: vina})."""
    runs: dict[tuple, dict] = defaultdict(dict)
    pool: dict[str, float] = {}
    pattern = os.path.join(root, "**", "round_*.json")
    for path in glob.glob(pattern, recursive=True):
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        if record.get("is_mock"):
            continue
        parts = Path(path).as_posix().split("/")
        # <root>/<group>/<repeat>/<attempt>/round_N.json
        key = tuple(parts[-4:-1])
        runs[key][record["round"]] = record.get("candidates") or []
        for cand in record.get("candidates") or []:
            score = (cand.get("dock") or {}).get("score")
            smiles = cand.get("smiles")
            if score is None or not smiles:
                continue
            if smiles not in pool or score < pool[smiles]:
                pool[smiles] = score
    return runs, pool


def extreme_value_curve(scores: list[float], sizes: list[int], draws: int = 4000) -> dict:
    """Mean / 5th / 95th percentile of best-of-k for k in `sizes`."""
    out = {}
    for k in sizes:
        k = min(k, len(scores))
        bests = [min(random.sample(scores, k)) for _ in range(draws)]
        out[k] = {
            "mean": round(statistics.mean(bests), 3),
            "p05": round(float(np.percentile(bests, 5)), 3),
            "p95": round(float(np.percentile(bests, 95)), 3),
        }
    return out


def audit_report(run_dir: str, draws: int = 4000) -> dict:
    """Compute the full search-vs-random audit for a run directory.

    Refactored out of ``main()`` (2026-09-27) so ``scripts/run_benchmark.py``
    can run the same audit as an experiment admission gate
    (REVIEW_MINIMAX_ADVICE_20260917 item 3). Returns the JSON-safe report
    dict; raises ValueError when there is nothing to audit.
    """
    runs, pool = load_runs(run_dir)
    if not pool:
        raise ValueError(f"no real (non-mock) candidates found under {run_dir}")
    scores = sorted(pool.values())
    report: dict = {"run_dir": run_dir, "n_unique_molecules": len(pool)}

    usable_range = float(np.percentile(scores, 25)) - scores[0]
    report["pool"] = {
        "n": len(pool),
        "best": round(scores[0], 3),
        "p25": round(float(np.percentile(scores, 25)), 3),
        "median": round(float(np.median(scores)), 3),
        "worst": round(scores[-1], 3),
        "usable_range": round(usable_range, 3),
    }

    # ---- 1) search vs random -------------------------------------------------
    observed_round_best = []
    for key in runs:
        for round_num in sorted(runs[key]):
            vals = [c["dock"]["score"] for c in runs[key][round_num]
                    if (c.get("dock") or {}).get("score") is not None]
            if vals:
                observed_round_best.append(min(vals))
    if not observed_round_best:
        raise ValueError("no scored rounds")
    observed_mean = statistics.mean(observed_round_best)
    per_round = statistics.median(
        len([c for c in runs[k][r] if (c.get("dock") or {}).get("score") is not None])
        for k in runs for r in runs[k]
    )
    per_round = int(per_round) or 15
    per_round = min(per_round, len(scores))
    curve = extreme_value_curve(scores, [per_round, per_round * 2, per_round * 3], draws)
    null = [min(random.sample(scores, per_round)) for _ in range(8000)]
    p_random_wins = float(np.mean(np.array(null) < observed_mean))
    report["search_vs_random"] = {
        "molecules_per_round": per_round,
        "observed_mean_best_of_round": round(observed_mean, 3),
        "random_best_of_round": curve,
        "p_random_beats_agent": round(p_random_wins, 4),
    }

    # ---- 2) direction of iteration ------------------------------------------
    worse = better = 0
    first_to_last = []
    for key in sorted(runs):
        seq = []
        for round_num in sorted(runs[key]):
            vals = [c["dock"]["score"] for c in runs[key][round_num]
                    if c.get("safety_gate_pass")
                    and (c.get("dock") or {}).get("score") is not None]
            seq.append(min(vals) if vals else None)
        if len(seq) >= 2 and seq[0] is not None and seq[-1] is not None:
            first_to_last.append(seq[-1] - seq[0])
            if seq[-1] > seq[0]:
                worse += 1
            else:
                better += 1
    if first_to_last:
        report["iteration_direction"] = {
            "worse": worse, "better": better,
            "mean_shift": round(statistics.mean(first_to_last), 3),
        }

    # how often is the very first round already the best of the whole run?
    first_is_best = 0
    counted = 0
    patience_hits = []
    for key in runs:
        seq = []
        for round_num in sorted(runs[key]):
            vals = [c["dock"]["score"] for c in runs[key][round_num]
                    if (c.get("dock") or {}).get("score") is not None]
            seq.append(min(vals) if vals else None)
        if not seq or seq[0] is None:
            continue
        counted += 1
        if all(v is None or v >= seq[0] for v in seq[1:]):
            first_is_best += 1
        best = None
        streak = max_streak = 0
        for value in seq:
            if value is None:
                continue
            if best is None or value < best:
                best, streak = value, 0
            else:
                streak += 1
                max_streak = max(max_streak, streak)
        patience_hits.append(max_streak)
    if counted:
        share = first_is_best / counted
        report["first_round_is_best"] = {
            "runs": counted,
            "share": round(share, 3),
            "max_no_improvement_streak": max(patience_hits) if patience_hits else None,
        }

    # per-arm paired shift
    per_group = {}
    for group in sorted({k[0] for k in runs}):
        pairs = []
        for key in runs:
            if key[0] != group:
                continue
            def best_safe(round_num):
                vals = [c["dock"]["score"] for c in runs[key].get(round_num, [])
                        if c.get("safety_gate_pass")
                        and (c.get("dock") or {}).get("score") is not None]
                return min(vals) if vals else None
            first, last = best_safe(0), best_safe(max(runs[key]))
            if first is not None and last is not None:
                pairs.append(last - first)
        if pairs:
            per_group[group] = {
                "n": len(pairs),
                "mean_shift": round(statistics.mean(pairs), 3),
            }
    if per_group:
        report["per_group_safe_shift"] = per_group

    # ---- 3) lineage / elitism ------------------------------------------------
    from rdkit import DataStructs
    similarities = []
    near = total = 0
    for key in sorted(runs):
        previous = None
        for round_num in sorted(runs[key]):
            best = None
            for cand in runs[key][round_num]:
                if cand.get("safety_gate_pass") and (cand.get("dock") or {}).get("score") is not None:
                    if best is None or cand["dock"]["score"] < best["dock"]["score"]:
                        best = cand
            if best is None:
                continue
            if previous is not None:
                fa, fb = morgan_fp(previous), morgan_fp(best["smiles"])
                if fa and fb:
                    tan = DataStructs.TanimotoSimilarity(fa, fb)
                    similarities.append(tan)
                    total += 1
                    if tan >= 0.6:
                        near += 1
            previous = best["smiles"]
    if similarities:
        report["lineage"] = {
            "mean_tanimoto": round(statistics.mean(similarities), 3),
            "share_ge_0.6": round(near / total, 3),
            "pairs": total,
        }

    # scaffold concentration
    from rdkit import Chem, RDLogger
    from rdkit.Chem.Scaffolds import MurckoScaffold
    RDLogger.DisableLog("rdApp.*")
    scaffolds = Counter()
    for smiles in pool:
        mol = Chem.MolFromSmiles(smiles)
        if mol:
            scaffolds[MurckoScaffold.MurckoScaffoldSmiles(mol=mol)] += 1
    total_mols = sum(scaffolds.values()) or 1
    top = scaffolds.most_common(5)
    report["scaffolds"] = {
        "unique": len(scaffolds),
        "top5_share": round(sum(c for _, c in top) / total_mols, 3),
        "top1_share": round(top[0][1] / total_mols, 3),
    }

    # ---- 4) vina / herg trade-off -------------------------------------------
    pairs = []
    for path in glob.glob(os.path.join(run_dir, "**", "round_*.json"), recursive=True):
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        if record.get("is_mock"):
            continue
        for cand in record.get("candidates") or []:
            vina = (cand.get("dock") or {}).get("score")
            herg = (cand.get("admet") or {}).get("herg_risk_score")
            if vina is None or herg is None:
                continue
            pairs.append((vina, herg, bool(cand.get("safety_gate_pass"))))
    if len(pairs) > 10:
        vina_arr = np.array([p[0] for p in pairs])
        herg_arr = np.array([p[1] for p in pairs])
        corr = float(np.corrcoef(vina_arr, herg_arr)[0, 1])
        safe = [p for p in pairs if p[2]]
        report["tradeoff"] = {
            "n": len(pairs),
            "pearson_vina_herg": round(corr, 3),
            "safety_pass_rate": round(len(safe) / len(pairs), 3),
            "best_vina_overall": round(float(vina_arr.min()), 3),
            "best_vina_safe": round(min(p[0] for p in safe), 3) if safe else None,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="directory holding <group>/<repeat>/<attempt>/round_*.json")
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--draws", type=int, default=4000)
    args = parser.parse_args()

    try:
        report = audit_report(args.run_dir, draws=args.draws)
    except ValueError as exc:
        print(str(exc))
        return 2

    print(f"pool: {report['pool']['n']} unique molecules with a Vina score")
    print(f"  best={report['pool']['best']:.3f}  p25={report['pool']['p25']:.3f}  "
          f"median={report['pool']['median']:.3f}  worst={report['pool']['worst']:.3f}")
    print(f"  usable range (best -> 25th pct) = {report['pool']['usable_range']:.3f} kcal/mol")

    print("\n=== 1) is the loop searching, or sampling? ===")
    svr = report["search_vs_random"]
    print(f"  molecules scored per round (median)      = {svr['molecules_per_round']}")
    print(f"  OBSERVED mean best-of-round              = {svr['observed_mean_best_of_round']:.3f}")
    for k, stats_ in svr["random_best_of_round"].items():
        print(f"  RANDOM best of {k:4d}                        = {stats_['mean']:.3f}  "
              f"[5th-95th {stats_['p05']:.3f} .. {stats_['p95']:.3f}]")
    print(f"  P(random draw beats the agent)           = {100 * svr['p_random_beats_agent']:.1f}%")
    if svr["p_random_beats_agent"] > 0.5:
        print("  -> random sampling beats the agent. The feedback path is NOT")
        print("     adding search signal; the bottleneck is the generator's raw")
        print("     output distribution, not the number of rounds.")

    if "iteration_direction" in report:
        it = report["iteration_direction"]
        print("\n=== 2) does the iteration move in a consistent direction? ===")
        print(f"  first->last round best-SAFE-Vina: worse {it['worse']} / better {it['better']}")
        print(f"  mean shift = {it['mean_shift']:+.3f} kcal/mol")
        flag = "coin flip — no consistent direction" if 0.25 < it["better"] / max(1, it["worse"] + it["better"]) < 0.75 \
            else "direction present"
        print(f"  -> {flag}")

    if "first_round_is_best" in report:
        frib = report["first_round_is_best"]
        print(f"\n  round-0 best IS the run-wide best in "
              f"{int(frib['share'] * frib['runs'])}/{frib['runs']} "
              f"runs = {100 * frib['share']:.0f}%")
        if frib.get("max_no_improvement_streak"):
            print(f"  longest no-improvement streak observed: max={frib['max_no_improvement_streak']}")

    if "per_group_safe_shift" in report:
        print("\n  per-arm paired round-0 -> last-round shift:")
        for group, pg in report["per_group_safe_shift"].items():
            print(f"    {group:22s} n={pg['n']:2d}  mean shift = {pg['mean_shift']:+.3f} kcal/mol")

    if "lineage" in report:
        lin = report["lineage"]
        print("\n=== 3) is there an elitist lineage (does round N+1 build on round N)? ===")
        print(f"  consecutive-round best Tanimoto: mean={lin['mean_tanimoto']:.3f}")
        print(f"  share >= 0.6 (analogue / local mutation) = {100 * lin['share_ge_0.6']:.1f}%")

    if "scaffolds" in report:
        sc = report["scaffolds"]
        print(f"\n  Murcko scaffolds: {sc['unique']} unique; "
              f"top-5 share = {100 * sc['top5_share']:.1f}%")

    if "tradeoff" in report:
        tr = report["tradeoff"]
        print("\n=== 4) Vina vs hERG trade-off ===")
        print(f"  n={tr['n']} molecules (continuous herg_risk_score only)")
        print(f"  Pearson r(vina, herg_risk) = {tr['pearson_vina_herg']:+.3f}")
        print(f"  safety-gate pass rate = {100 * tr['safety_pass_rate']:.1f}%")
        print(f"  best Vina overall    = {tr['best_vina_overall']:.3f}")
        if tr.get("best_vina_safe") is not None:
            print(f"  best Vina among SAFE = {tr['best_vina_safe']:.3f}"
                  f"   (cost of the safety gate: "
                  f"{tr['best_vina_safe'] - tr['best_vina_overall']:+.3f} kcal/mol)")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\n[json] {args.json_out}")
    return 0


def morgan_fp(smiles: str):
    from rdkit import Chem, RDLogger
    from rdkit.Chem import AllChem
    RDLogger.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(smiles)
    return AllChem.GetMorganFingerprintAsBitVect(mol, 2, 2048) if mol else None


if __name__ == "__main__":
    sys.exit(main())
