"""Build the final 3-repetition stability summary from pre-fix, fix, and r2."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "runs/samples"

scout = json.loads((SAMPLES / "parent_scout_20260920.json").read_text(encoding="utf-8"))
pre = json.loads((SAMPLES / "diagnostic_2d_stability_20260920_rows.json").read_text(encoding="utf-8"))
fix = json.loads((SAMPLES / "diagnostic_2d_fixed_20260920_rows.json").read_text(encoding="utf-8"))
r2 = json.loads((SAMPLES / "diagnostic_2d_r2_20260921_rows.json").read_text(encoding="utf-8"))
gate = json.loads((SAMPLES / "minimax_connectivity_20260921_v2_summary.json").read_text(encoding="utf-8"))

summary = {
    "schema_version": 1,
    "experiment": "diagnostic_2d_multi_parent_stability_3x",
    "created_utc": "2026-09-21T12:00:00+00:00",
    "protocol_id": "diagnostic_2d_multi_parent_stability_20260920",
    "purpose": (
        "After finding and fixing the decision-loop defect (2026-09-20) and "
        "documenting the missing variance estimate (single run per arm), "
        "this summary records three observations per parent per arm "
        "covering two frozen versions of the protocol."
    ),
    "parent_selection": {
        "method": "scripts/scout_parents.py, fully offline arithmetic",
        "selection_made_before_any_model_call": True,
        "fragments": ["C", "N", "O", "F", "Cl", "CCC", "CCO", "C(F)(F)F", "C#N", "C(=O)N"],
        "catalogue_rule": "every fragment in SCOUT_FRAGMENTS attached at every heavy atom of the parent",
        "usable_parents": len(scout["usable_parents"]),
        "total_scouted": len(scout["results"]),
        "selected": [{"name": p["name"], "parent_smiles": p["parent_smiles"],
                      "reachable_qualifying_products": p["qualifying_products"]}
                     for p in scout["usable_parents"][:3]],
    },
    "frozen_versions": {
        "pre_fix": {
            "rows_file": "runs/samples/diagnostic_2d_stability_20260920_rows.json",
            "description": "Frozen version before the choose_strategy repeat fix.",
            "distinct_defect": "aniline agent found 2 qualifying molecules but re-issued choose_strategy for the already-selected planned_s2, was stopped as decision_loop",
        },
        "fix": {
            "rows_file": "runs/samples/diagnostic_2d_fixed_20260920_rows.json",
            "description": "Same frozen version plus the choose_strategy one-shot enforcement and the qualifying_candidate_found stage. Re-verified the fix worked.",
        },
        "r2": {
            "rows_file": "runs/samples/diagnostic_2d_r2_20260921_rows.json",
            "description": "A second repetition of the fixed frozen version on the same three parents to add an observation. Token plan was exhausted earlier in the day and recovered (gate=passed, 3/3, 12:01 UTC).",
        },
    },
    "connectivity_gate_for_r2": {
        "summary_file": "runs/samples/minimax_connectivity_20260921_v2_summary.json",
        "gate": gate.get("gate"), "successful": gate.get("successful"), "attempted": gate.get("attempted"),
        "clients_created": gate.get("clients_created"), "retried_requests": gate.get("retried_requests"),
    },
    "three_observations_per_arm": {
        "selection_rule": (
            "All three observations are reported in chronological order. No "
            "arm is re-run, no observation is dropped, and the best outcome "
            "is not singled out. pre_fix and fix are different frozen versions "
            "(different source hashes), so the comparability across them is "
            "limited to 'does the fix work'."
        ),
        "agent_arm": [
            {"parent": "phenol", "parent_smiles": "Oc1ccccc1",
             "pre_fix": next(r for r in pre if r["parent"] == "phenol" and r["arm"] == "agent"),
             "fix":     next(r for r in fix if r["parent"] == "phenol" and r["arm"] == "agent"),
             "r2":      next(r for r in r2  if r["parent"] == "phenol" and r["arm"] == "agent")},
            {"parent": "aniline", "parent_smiles": "Nc1ccccc1",
             "pre_fix": next(r for r in pre if r["parent"] == "aniline" and r["arm"] == "agent"),
             "fix":     next(r for r in fix if r["parent"] == "aniline" and r["arm"] == "agent"),
             "r2":      next(r for r in r2  if r["parent"] == "aniline" and r["arm"] == "agent")},
            {"parent": "toluene", "parent_smiles": "Cc1ccccc1",
             "pre_fix": next(r for r in pre if r["parent"] == "toluene" and r["arm"] == "agent"),
             "fix":     next(r for r in fix if r["parent"] == "toluene" and r["arm"] == "agent"),
             "r2":      next(r for r in r2  if r["parent"] == "toluene" and r["arm"] == "agent")},
        ],
    },
    "observations": [
        {
            "what": "Decision loop after finding qualifying candidates",
            "evidence": "aniline pre_fix terminated as decision_loop with 2 qualifying molecules unreported; aniline fix and aniline r2 both terminated cleanly.",
            "status": "Defect eliminated. Two clean observations after the fix.",
        },
        {
            "what": "Toluene goal_met is reproducible on the fixed version",
            "evidence": "toluene fix q=1 best=0.01874; toluene r2 q=3 best=0.01874. Same best delta across two runs.",
            "status": "Reproducible at the same molecule and delta.",
        },
        {
            "what": "Phenol reaches goal_met in r2 after failing in the earlier two",
            "evidence": "phenol pre_fix and fix both evaluation_budget_exhausted q=0; phenol r2 goal_met q=1 best=0.01478.",
            "status": (
                "The first goal_met for phenol happened on the third observation. "
                "Two negative observations are not zero risk: phenol does sometimes "
                "need the full edit budget to reach the threshold. r2 is reported as "
                "a single observation; the larger claim needs more repetitions."
            ),
        },
        {
            "what": "Rule arm never finds qualifying on any parent in any run",
            "evidence": "0 qualifying across 9 rule-arm runs (3 parents x 3 versions).",
            "status": "Weak baseline, but consistent.",
        },
        {
            "what": "Transport remains stable",
            "evidence": "9 agent-arm runs in this summary, 0 network failures, 0 network retries on the latest two versions; pre_fix has 0 too.",
            "status": "Stable.",
        },
    ],
    "honest_limitations": [
        "Two observations per parent on the fixed version, not three. pre_fix and fix are different frozen versions and are not directly comparable as stability repeats.",
        "One run per parent per version means no variance estimate and no statistical test.",
        "Rule arm found 0/9; it is a weak baseline and cannot be used to claim the agent is better.",
        "phenol r2 reached goal_met once after two failures; this could be noise.",
        "The parent-derived catalogue is larger than the original phenol catalogue, so cross-version comparison is about mechanism, not effect size.",
        "No docking, no biological validation, and the 0.01 threshold is unchanged.",
    ],
}

out = SAMPLES / "diagnostic_2d_stability_3x_20260921_summary.json"
out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote", out.relative_to(ROOT))
