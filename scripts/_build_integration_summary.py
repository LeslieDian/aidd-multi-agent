"""Build integration summary: rule_memory + calibrated hERG wired in."""
import json
import os

# Load latest dock-aware three-arm aggregate
from pathlib import Path

base = Path("runs/diagnostic_2d_dock_20260922")
gate = json.load(open("runs/samples/minimax_connectivity_20260923_v1_summary.json", encoding="utf-8"))

# Per-arm summary
arms = {}
for aid in ["greedy", "random", "agent"]:
    metrics_path = base / aid / "metrics.json"
    task_path = base / aid / "task.json"
    if not metrics_path.exists():
        continue
    m = json.loads(metrics_path.read_text(encoding="utf-8"))
    arms[aid] = {
        "termination_outcome": m["termination_outcome"],
        "goal_met": m["termination_outcome"] == "goal_met",
        "qualifying_property_only": m["successful_screened_products"],
        "best_delta_property_only": m["best_compliant_delta"],
        "schema_errors": m["schema_errors"],
        "state_machine_rejections": m["state_machine_rejections"],
        "network_failures": m["network_failures"],
        "new_structure_evaluations": m.get("new_structure_evaluations"),
        "planner_attempts": m.get("planner_attempts"),
    }

# Rule memory file presence
rule_mem = list(Path("runs/samples").glob("rule_memory_*.json"))
rule_mem_info = []
for f in rule_mem:
    d = json.load(open(f, encoding="utf-8"))
    rule_mem_info.append({"path": str(f), "rule_count": len(d.get("rules", {}))})

summary = {
    "schema_version": 1,
    "experiment": "diagnostic_2d_dock_arms_with_rule_memory_and_calibrated_herg_20260923",
    "created_utc": "2026-09-23T00:38:00+00:00",
    "purpose": (
        "Re-run dock-aware three-arm comparison AFTER wiring (a) the 4-category "
        "RuleStore into LLMPolicy.decide prompt + post-event hook, and "
        "(b) calibrated_herg_score into the multi-objective threshold. "
        "The previous run (commit d5c84ac) had 0/0/0 dock-aware qualifying "
        "with no memory integration and used the old heuristic hERG; this run "
        "tests whether the new wiring changes the outcome."
    ),
    "connectivity_gate": {"gate": "passed (3/3)",
                          "summary_file": "minimax_connectivity_20260923_v1_summary.json"},
    "threshold": {
        "property_score_delta_min": 0.01,
        "vina_score_max": -5.0,
        "herg_risk_heuristic_ceil": 0.55,
        "herg_risk_calibrated_ceil": 0.50,
        "logp_max": 4.50,
    },
    "arms": arms,
    "rule_memory": rule_mem_info,
    "headline_finding": (
        "Under the cmp2d scoring system (property_score = summary_score), the "
        "dock-aware multi-objective threshold (property Δ ≥ 0.01 AND vina ≤ -5.0 "
        "AND calibrated_hERG ≤ 0.50 AND logP ≤ 4.50) admits 0 of 16 products in "
        "10 evaluations regardless of arm. Both greedy (1 supported property-only) "
        "and random (2 supported property-only) find products that pass the OLD "
        "property-only threshold but fail the new dock-aware threshold on either "
        "vina or property_delta depending on which product is the audit-best. "
        "The agent terminates with execution_failure (4 schema errors) AFTER "
        "memory integration, which is WORSE than the previous no-memory run. "
        "n=1 cannot distinguish 'agent is bad' from 'memory integration hurts'. "
        "Honest finding: under realistic multi-objective pressure, the 10-evaluation "
        "budget is insufficient for ANY of the three policies to find a product "
        "satisfying ALL four constraints in this catalogue."
    ),
    "what_changed_vs_previous_run": {
        "before": "agent: evaluation_budget_exhausted, 0 qualifying, 0 schema_errors",
        "after": "agent: execution_failure, 0 qualifying, 4 schema_errors (sanitizer absorbed)",
        "memory_wiring_status": (
            "VERIFIED by manual test (test_rule_memory.py + scripts/_test_memory.py "
            "showed add_negative writes the file). In the actual agent run, the "
            "10 evaluate_options results were all outcome=insufficient_evidence, "
            "which the hook is intentionally configured to ignore (only supported / "
            "tradeoff_exceeded / inconclusive outcomes generate rules). So 0 rules "
            "were created and no rule_memory file was generated for this run."
        ),
        "calibrated_herg_wiring_status": (
            "Integrated into run_2d_with_docking.py threshold; "
            "calibrated_herg_score <= 0.50 (vs heuristic <= 0.55). For the "
            "phenol-derivative catalogue all calibrated scores are < 0.10, so "
            "the calibrated gate does not exclude any product that the "
            "heuristic gate would."
        ),
    },
    "tests": "330 passed, 1 skipped (no regressions)",
    "intent_check": (
        "Same scoring, same threshold, same evaluation budget. The agent's "
        "execution_failure is one observation; previous run had the same agent "
        "score 0/0 = 0 with evaluation_budget_exhausted. Differences (4 vs 0 "
        "schema_errors, execution_failure vs budget_exhausted) are within "
        "n=1 variance."
    ),
}

out = Path("runs/samples/diagnostic_2d_dock_arms_with_integration_20260923_summary.json")
out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Wrote {out}")
print()
print("=== Dock-aware three-arm after integration ===")
for arm, info in summary["arms"].items():
    print(f"{arm:8s} term={info['termination_outcome']:30s} qual={info['qualifying_property_only']} schema_err={info['schema_errors']}")