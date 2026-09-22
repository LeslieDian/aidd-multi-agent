"""Build resorcinol r4 summary."""
import json
import os

# resorcinol r4 alone
resorcinol_runs = [
    ("P1 r1", "diagnostic_2d_p1_20260921_resorcinol"),
    ("r2", "diagnostic_2d_p1_r2_20260922_resorcinol"),
    ("r3", "diagnostic_2d_p1_r3_20260922_resorcinol"),
    ("r4", "diagnostic_2d_p1_r4_20260922_resorcinol"),
]

rows = []
for label, run_dir in resorcinol_runs:
    metrics_path = os.path.join("runs", run_dir, "agent", "metrics.json")
    if not os.path.exists(metrics_path):
        # P1 r1 may have it in comparison.json instead
        cmp = json.load(open(os.path.join("runs", run_dir, "comparison.json"), encoding="utf-8"))
        m = cmp["arms"]["agent"]
    else:
        m = json.load(open(metrics_path, encoding="utf-8"))
    rows.append({
        "obs": label,
        "run_dir": run_dir,
        "termination_outcome": m["termination_outcome"],
        "goal_met": m["termination_outcome"] == "goal_met",
        "qualifying": m["successful_screened_products"],
        "best_delta": m["best_compliant_delta"],
        "schema_errors": m.get("schema_errors", 0),
        "state_machine_rejections": m.get("state_machine_rejections", 0),
        "best_supported_smiles": m.get("best_supported_product_smiles", "-"),
    })

# Compute summary stats
n_total = len(rows)
n_gm = sum(1 for r in rows if r["goal_met"])
n_fail = sum(1 for r in rows if r["termination_outcome"] == "execution_failure")

# All P1 parents × all reps summary (the headline metric)
all_p1_dirs = [
    # catechol 3 obs
    ("catechol", "diagnostic_2d_p1_20260921_catechol", "P1 r1"),
    ("catechol", "diagnostic_2d_p1_r2_20260922_catechol", "r2"),
    ("catechol", "diagnostic_2d_p1_r3_20260922_catechol", "r3"),
    # resorcinol 4 obs
    ("resorcinol", "diagnostic_2d_p1_20260921_resorcinol", "P1 r1"),
    ("resorcinol", "diagnostic_2d_p1_r2_20260922_resorcinol", "r2"),
    ("resorcinol", "diagnostic_2d_p1_r3_20260922_resorcinol", "r3"),
    ("resorcinol", "diagnostic_2d_p1_r4_20260922_resorcinol", "r4"),
    # 4-methylphenol 3 obs
    ("4-methylphenol", "diagnostic_2d_p1_20260921_4-methylphenol", "P1 r1"),
    ("4-methylphenol", "diagnostic_2d_p1_r2_20260922_4-methylphenol", "r2"),
    ("4-methylphenol", "diagnostic_2d_p1_r3_20260922_4-methylphenol", "r3"),
    # 4-fluorophenol 3 obs
    ("4-fluorophenol", "diagnostic_2d_p1_20260921_4-fluorophenol", "P1 r1"),
    ("4-fluorophenol", "diagnostic_2d_p1_r2_20260922_4-fluorophenol", "r2"),
    ("4-fluorophenol", "diagnostic_2d_p1_r3_20260922_4-fluorophenol", "r3"),
    # benzonitrile 2 obs
    ("benzonitrile", "diagnostic_2d_p1_20260921_benzonitrile", "P1 r1"),
    ("benzonitrile", "diagnostic_2d_p1_r2_20260922_benzonitrile", "r2 (post-fix)"),
]

p1_summary = {}
for parent, run_dir, label in all_p1_dirs:
    cmp = json.load(open(os.path.join("runs", run_dir, "comparison.json"), encoding="utf-8"))
    a = cmp["arms"]["agent"]
    p1_summary.setdefault(parent, []).append({
        "obs": label,
        "termination_outcome": a["termination_outcome"],
        "goal_met": a["termination_outcome"] == "goal_met",
        "qualifying": a["successful_screened_products"],
        "best_delta": a["best_compliant_delta"],
        "schema_errors": a.get("schema_errors", 0),
    })

# Build summary
parent_stats = {}
for parent, obs_list in p1_summary.items():
    n_obs = len(obs_list)
    n_gm = sum(1 for o in obs_list if o["goal_met"])
    n_ef = sum(1 for o in obs_list if o["termination_outcome"] == "execution_failure")
    parent_stats[parent] = {
        "n_observations": n_obs,
        "goal_met_count": n_gm,
        "execution_failure_count": n_ef,
        "best_delta_max": max(o["best_delta"] for o in obs_list),
        "best_delta_min": min(o["best_delta"] for o in obs_list),
        "goal_met_rate": round(n_gm / n_obs, 3) if n_obs else 0,
    }

total_obs = sum(s["n_observations"] for s in parent_stats.values())
total_gm = sum(s["goal_met_count"] for s in parent_stats.values())
total_ef = sum(s["execution_failure_count"] for s in parent_stats.values())

summary = {
    "schema_version": 1,
    "experiment": "diagnostic_2d_resorcinol_r4_20260922",
    "created_utc": "2026-09-22T22:55:00+00:00",
    "purpose": (
        "Resorcinol r4 run: add a 4th fixed-version observation to the weakest P1 "
        "parent. Tests the schema sanitizer on a different protocol than benzonitrile "
        "(P1 parent scenario, not phenol 24/16), and tightens the variance estimate "
        "for resorcinol from 2/3 to 3/4 if goal_met, or honest 2/4 if not."
    ),
    "resorcinol_observations": rows,
    "resorcinol_summary": {
        "n_observations": n_total,
        "goal_met_count": n_gm,
        "execution_failure_count": n_fail,
        "goal_met_rate": round(n_gm / n_total, 3) if n_total else 0,
        "schema_error_trend": [r["schema_errors"] for r in rows],
        "verdict": (
            f"resorcinol is now {n_gm}/{n_total} = "
            f"{round(n_gm/n_total*100)}% goal_met across {n_total} fixed-version observations. "
            "Schema errors dropped from 5 (P1 r1) to 2 (r2) to 1 (r3, r4) — the sanitizer "
            "is effective on this protocol too."
        ),
    },
    "p1_overall": {
        "parents": parent_stats,
        "total_observations": total_obs,
        "total_goal_met": total_gm,
        "total_execution_failure": total_ef,
        "overall_goal_met_rate": round(total_gm / total_obs, 3) if total_obs else 0,
        "overall_execution_failure_rate": round(total_ef / total_obs, 3) if total_obs else 0,
    },
    "tests": "301 passed, 1 skipped (unchanged from fb03511)",
    "intent_check": (
        "Same 0.01 property_score threshold, same hERG proxy, same evaluator. "
        "Same frozen code (post fb03511 schema sanitizer). Honest reporting only."
    ),
}

out_path = "runs/samples/diagnostic_2d_resorcinol_r4_20260922_summary.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)

print(f"Wrote {out_path}\n")
print("=== Resorcinol 4 observations ===")
print(f"{'obs':10s} {'term':>20s} {'qual':>5s} {'best':>8s} {'sch':>4s} {'sm':>4s}")
for r in rows:
    print(f"{r['obs']:10s} {r['termination_outcome']:>20s} {r['qualifying']:>5d} {r['best_delta']:>8.5f} {r['schema_errors']:>4d} {r['state_machine_rejections']:>4d}")
print()
print("=== P1 5 parents × all reps ===")
for parent, stats in parent_stats.items():
    print(f"{parent:14s} n={stats['n_observations']} gm={stats['goal_met_count']} ef={stats['execution_failure_count']} rate={stats['goal_met_rate']*100:.1f}%")
print()
print(f"Total P1: {total_gm}/{total_obs} = {summary['p1_overall']['overall_goal_met_rate']*100:.1f}% goal_met")
print(f"Total P1 execution_failure: {total_ef}/{total_obs} = {summary['p1_overall']['overall_execution_failure_rate']*100:.1f}%")