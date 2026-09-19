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


def morgan_fp(smiles: str):
    from rdkit import Chem, RDLogger
    from rdkit.Chem import AllChem
    RDLogger.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(smiles)
    return AllChem.GetMorganFingerprintAsBitVect(mol, 2, 2048) if mol else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="directory holding <group>/<repeat>/<attempt>/round_*.json")
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--draws", type=int, default=4000)
    args = parser.parse_args()

    runs, pool = load_runs(args.run_dir)
    if not pool:
        print(f"no real (non-mock) candidates found under {args.run_dir}")
        return 2
    scores = sorted(pool.values())
    report: dict = {"run_dir": args.run_dir, "n_unique_molecules": len(pool)}

    print(f"pool: {len(pool)} unique molecules with a Vina score")
    print(f"  best={scores[0]:.3f}  p25={np.percentile(scores,25):.3f}  "
          f"median={np.median(scores):.3f}  worst={scores[-1]:.3f}")
    usable_range = np.percentile(scores, 25) - scores[0]
    print(f"  usable range (best -> 25th pct) = {usable_range:.3f} kcal/mol")
    report["pool"] = {
        "n": len(pool),
        "best": round(scores[0], 3),
        "p25": round(float(np.percentile(scores, 25)), 3),
        "median": round(float(np.median(scores)), 3),
        "worst": round(scores[-1], 3),
        "usable_range": round(float(usable_range), 3),
    }

    # ---- 1) search vs random -------------------------------------------------
    print("\n=== 1) is the loop searching, or sampling? ===")
    observed_round_best = []
    for key in runs:
        for round_num in sorted(runs[key]):
            vals = [c["dock"]["score"] for c in runs[key][round_num]
                    if (c.get("dock") or {}).get("score") is not None]
            if vals:
                observed_round_best.append(min(vals))
    if not observed_round_best:
        print("  no scored rounds")
        return 2
    observed_mean = statistics.mean(observed_round_best)
    per_round = statistics.median(
        len([c for c in runs[k][r] if (c.get("dock") or {}).get("score") is not None])
        for k in runs for r in runs[k]
    )
    per_round = int(per_round) or 15
    curve = extreme_value_curve(scores, [per_round, per_round * 2, per_round * 3], args.draws)
    null = [min(random.sample(scores, per_round)) for _ in range(8000)]
    p_random_wins = float(np.mean(np.array(null) < observed_mean))

    print(f"  molecules scored per round (median)      = {per_round}")
    print(f"  OBSERVED mean best-of-round              = {observed_mean:.3f}  "
          f"(n={len(observed_round_best)} rounds)")
    for k, stats_ in curve.items():
        print(f"  RANDOM best of {k:4d}                        = {stats_['mean']:.3f}  "
              f"[5th-95th {stats_['p05']:.3f} .. {stats_['p95']:.3f}]")
    print(f"  P(random draw beats the agent)           = {100 * p_random_wins:.1f}%")
    if p_random_wins > 0.5:
        print("  -> random sampling beats the agent. The feedback path is NOT")
        print("     adding search signal; the bottleneck is the generator's raw")
        print("     output distribution, not the number of rounds.")
    report["search_vs_random"] = {
        "molecules_per_round": per_round,
        "observed_mean_best_of_round": round(observed_mean, 3),
        "random_best_of_round": curve,
        "p_random_beats_agent": round(p_random_wins, 4),
    }

    # ---- 2) direction of iteration ------------------------------------------
    print("\n=== 2) does the iteration move in a consistent direction? ===")
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
        print(f"  first->last round best-SAFE-Vina: worse {worse} / better {better}")
        print(f"  mean shift = {statistics.mean(first_to_last):+.3f} kcal/mol")
        flag = "coin flip — no consistent direction" if 0.25 < better / (worse + better) < 0.75 \
            else "direction present"
        print(f"  -> {flag}")
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
        print(f"\n  round-0 best IS the run-wide best in {first_is_best}/{counted} "
              f"runs = {100 * share:.0f}%")
        observed_rounds = max(len(runs[k]) for k in runs)
        patience = 3
        if patience_hits:
            mean_streak = statistics.mean(patience_hits)
            print(f"  longest no-improvement streak observed within {observed_rounds} rounds: "
                  f"mean={mean_streak:.2f} max={max(patience_hits)}")
            print("  -> if early_stop patience is N and rounds are raised far beyond what you")
            print("     just ran, the patience counter usually fires long before max_rounds.")
            print("     Check LoopController's signal: if it reads an all-candidates best")
            print("     (ignoring the constraint), it is measuring progress the loop cannot use.")
        report["first_round_is_best"] = {
            "runs": counted,
            "share": round(share, 3),
            "max_no_improvement_streak": max(patience_hits) if patience_hits else None,
        }

    # per-arm paired shift
    print("\n  per-arm paired round-0 -> last-round shift:")
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
            print(f"    {group:22s} n={len(pairs):2d}  mean shift = "
                  f"{statistics.mean(pairs):+.3f} kcal/mol")

    # ---- 3) lineage / elitism ------------------------------------------------
    print("\n=== 3) is there an elitist lineage (does round N+1 build on round N)? ===")
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
        print(f"  consecutive-round best Tanimoto: mean={statistics.mean(similarities):.3f} "
              f"median={statistics.median(similarities):.3f}")
        print(f"  share >= 0.6 (analogue / local mutation) = {near}/{total} "
              f"= {100 * near / total:.1f}%")
        print("  -> low share means there is no explicit parent/elite mechanism:")
        print("     each round re-samples instead of climbing from the best so far.")
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
    print(f"\n  Murcko scaffolds: {len(scaffolds)} unique across {total_mols} molecules")
    for smiles, count in top:
        print(f"    {count:4d} ({100 * count / total_mols:5.1f}%)  {smiles}")
    print(f"  -> top scaffold = {100 * top[0][1] / total_mols:.1f}% of the pool; "
          f"top-5 = {100 * sum(c for _, c in top) / total_mols:.1f}%")
    report["scaffolds"] = {
        "unique": len(scaffolds),
        "top5_share": round(sum(c for _, c in top) / total_mols, 3),
        "top1_share": round(top[0][1] / total_mols, 3),
    }

    # ---- 4) vina / herg trade-off -------------------------------------------
    print("\n=== 4) Vina vs hERG trade-off ===")
    pairs = []
    for path in glob.glob(os.path.join(args.run_dir, "**", "round_*.json"), recursive=True):
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
        print(f"  n={len(pairs)} molecules (continuous herg_risk_score only)")
        print(f"  Pearson r(vina, herg_risk) = {corr:+.3f}")
        print(f"  safety-gate pass rate = {100 * len(safe) / len(pairs):.1f}%")
        print(f"  best Vina overall    = {vina_arr.min():.3f}")
        if safe:
            print(f"  best Vina among SAFE = {min(p[0] for p in safe):.3f}"
                  f"   (cost of the safety gate: "
                  f"{min(p[0] for p in safe) - vina_arr.min():+.3f} kcal/mol)")
            print("  -> if the safe optimum is within docking noise of the unsafe"
                  " optimum, the safety regression is a *selection* bug, not an"
                  " unavoidable trade-off.")
        report["tradeoff"] = {
            "n": len(pairs),
            "pearson_vina_herg": round(corr, 3),
            "safety_pass_rate": round(len(safe) / len(pairs), 3),
            "best_vina_overall": round(float(vina_arr.min()), 3),
            "best_vina_safe": round(min(p[0] for p in safe), 3) if safe else None,
        }

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\n[json] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
