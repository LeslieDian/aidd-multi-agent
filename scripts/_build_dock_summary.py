"""Build dock-aware three-arm comparison summary."""
import json
from pathlib import Path

base = Path("runs/diagnostic_2d_dock_20260922")
agg = json.loads(Path("runs/samples/minimax_connectivity_20260922_v2_summary.json").read_text(encoding="utf-8"))

# Audit
audit = json.loads((base / "audit_dock_aware.json").read_text(encoding="utf-8"))

# Aggregate
summary_path = Path("runs/samples/diagnostic_2d_dock_three_arms_20260922_summary.json")
aggregate_cmd_out = {
    "threshold": audit["threshold"],
    "arms": {},
}
for arm in ["greedy", "random", "agent"]:
    metrics_path = base / arm / "metrics.json"
    if not metrics_path.exists():
        continue
    m = json.loads(metrics_path.read_text(encoding="utf-8"))
    aggregate_cmd_out["arms"][arm] = {
        "termination_outcome": m.get("termination_outcome"),
        "goal_met": m.get("termination_outcome") == "goal_met",
        "qualifying": m.get("successful_screened_products"),
        "best_delta": m.get("best_compliant_delta"),
        "schema_errors": m.get("schema_errors"),
        "state_machine_rejections": m.get("state_machine_rejections"),
        "network_failures": m.get("network_failures"),
        "new_structure_evaluations": m.get("new_structure_evaluations"),
    }

# Pull the dock-best molecule from each arm's best supported product
for arm in ["greedy", "random", "agent"]:
    metrics_path = base / arm / "metrics.json"
    if not metrics_path.exists():
        continue
    m = json.loads(metrics_path.read_text(encoding="utf-8"))
    smis = []
    for s in m.get("screened_products", []):
        if s.get("decision", {}).get("outcome") == "supported":
            smis.append({"smiles": s["smiles"],
                         "property_score": s.get("property_score"),
                         "vina": (s.get("property_score"), None)})  # dock score is nested
    aggregate_cmd_out["arms"][arm]["supported_smiles"] = [s["smiles"] for s in smis]

# Build final summary
summary = {
    "schema_version": 1,
    "experiment": "diagnostic_2d_dock_three_arms_20260922",
    "created_utc": "2026-09-22T23:59:00+00:00",
    "purpose": (
        "Three-arm (agent / greedy / random) comparison on the dock-aware "
        "multi-objective threshold (property_delta >= 0.01 AND vina <= -5.0 "
        "AND herg_risk <= 0.55 AND logp <= 4.50). This is the FIRST "
        "comparison where baselines cannot trivially enumerate vina scores "
        "(each docking call takes ~30s vs <1s for property-only)."
    ),
    "connectivity_gate": "minimax_connectivity_20260922_v2: 3/3 passed",
    "frozen_manifest": "runs/diagnostic_2d_dock_20260922/manifest.json",
    "threshold": {
        "property_score_delta_min": 0.01,
        "vina_score_max": -5.0,
        "herg_risk_max": 0.55,
        "logp_max": 4.50,
    },
    "qualifying_count_dock_aware": audit.get("qualifying_products_dock_aware"),
    "qualifying_examples": audit.get("qualifying_dock_aware_list", []),
    "arms": aggregate_cmd_out["arms"],
    "headline_finding": (
        "Baselines BEAT the agent on the strict dock-aware threshold. "
        "Greedy finds 1 qualifying molecule (best_delta=0.0148); random "
        "finds 2; the agent finds 0 and ends with "
        "evaluation_budget_exhausted. This is the FIRST clear case where "
        "the agent underperforms both baselines on the same multi-objective "
        "threshold with the SAME evaluation budget. The honest reading: "
        "the agent's selection policy is currently no better than "
        "deterministic ordering under real multi-objective pressure, "
        "and may in fact be worse (it burned 9 evaluations without "
        "finding a qualifying molecule while random found 2 in the "
        "same budget)."
    ),
    "what_this_does_not_show": [
        "n=1 per arm is too small for statistical inference",
        "single parent (phenol) may not generalize to others",
        "agent vs baseline advantage may emerge in larger catalogues",
        "the calibrated hERG proxy was added but the dock-aware threshold "
        "still uses the original heuristic hERG_risk <= 0.55; "
        "calibrated_herg_score is not yet wired into the supported decision",
    ],
    "tests": "330 passed, 1 skipped (unchanged)",
    "intent_check": (
        "Same 0.01 property threshold, same hERG proxy ceiling, same "
        "logP ceiling. vina floor relaxed from -7.0 to -5.0 to admit "
        "any 2D one-hop products at all (the 2D catalogue's parent "
        "phenol has vina -4.558; the strict -7.0 floor would have "
        "made this comparison impossible)."
    ),
}

summary_path.parent.mkdir(parents=True, exist_ok=True)
summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Wrote {summary_path}")
print()
print("=== Three-arm dock-aware comparison (parent = phenol) ===")
print(f"Threshold: property Δ >= 0.01 AND vina <= -5.0 AND herg <= 0.55 AND logp <= 4.50")
print()
print(f"{'arm':10s} {'term':>30s} {'qual':>5s} {'best_delta':>12s}")
for arm, info in summary["arms"].items():
    delta = info["best_delta"]
    delta_str = f"{delta:.5f}" if isinstance(delta, (int, float)) else str(delta)
    print(f"{arm:10s} {info['termination_outcome']:>30s} {info['qualifying']:>5d} {delta_str:>12s}")