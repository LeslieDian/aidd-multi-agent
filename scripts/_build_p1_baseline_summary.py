"""Build comprehensive 8-parent baseline + agent summary."""
import json
import os

parents_p1 = [
    ("catechol", "Oc1ccccc1O", "parent", "diagnostic_2d_p1_20260921_catechol"),
    ("resorcinol", "Oc1cccc(O)c1", "parent", "diagnostic_2d_p1_20260921_resorcinol"),
    ("4-methylphenol", "Cc1ccc(O)cc1", "parent", "diagnostic_2d_p1_20260921_4-methylphenol"),
    ("4-fluorophenol", "Oc1ccc(F)cc1", "parent", "diagnostic_2d_p1_20260921_4-fluorophenol"),
    ("benzonitrile", "N#Cc1ccccc1", "parent", "diagnostic_2d_p1_20260921_benzonitrile"),
]

parents_orig = [
    ("phenol", "Oc1ccccc1", "phenol", "diagnostic_2d_baseline_20260921_phenol"),
    ("aniline", "Nc1ccccc1", "phenol", "diagnostic_2d_baseline_20260921_aniline"),
    ("toluene", "Cc1ccccc1", "phenol", "diagnostic_2d_baseline_20260921_toluene"),
]

agent_obs_orig = json.load(
    open("runs/samples/diagnostic_2d_stability_4x_20260921_summary.json", encoding="utf-8")
)["agent_arm_four_observations"]


def get_arm(run_dir, arm):
    path = os.path.join("runs", run_dir, arm, "metrics.json")
    return json.load(open(path)) if os.path.exists(path) else None


def load_agent_p1(run_dir):
    base = os.path.join("runs", run_dir)
    cmp = json.load(open(os.path.join(base, "comparison.json")))
    return cmp["arms"]["agent"]


def build_row(name, smiles, scenario, run_dir, source_set, agent_data=None):
    g = get_arm(run_dir, "greedy")
    r = get_arm(run_dir, "random")
    row = {
        "parent": name,
        "parent_smiles": smiles,
        "scenario": scenario,
        "run_dir": run_dir,
        "source_set": source_set,
        "greedy": {
            "qualifying": g["successful_screened_products"],
            "best_delta": g["best_compliant_delta"],
            "termination": g["termination_outcome"],
        },
        "random": {
            "qualifying": r["successful_screened_products"],
            "best_delta": r["best_compliant_delta"],
            "termination": r["termination_outcome"],
        },
    }
    if agent_data is not None:
        if isinstance(agent_data, list):
            obs_summary = []
            quals = []
            bests = []
            for obs in agent_data:
                quals.append(obs["qualifying"])
                bests.append(obs["best_delta"])
                obs_summary.append({
                    "arm": obs.get("arm", "agent"),
                    "termination_outcome": obs["termination_outcome"],
                    "qualifying": obs["qualifying"],
                    "best_delta": obs["best_delta"],
                })
            row["agent"] = {
                "observations": obs_summary,
                "n_observations": len(obs_summary),
                "goal_met_count": sum(1 for o in obs_summary if o["termination_outcome"] == "goal_met"),
                "execution_failure_count": sum(1 for o in obs_summary if o["termination_outcome"] == "execution_failure"),
                "qualifying_total": sum(quals),
                "best_delta_max": max(bests),
            }
        else:
            row["agent"] = {
                "observations": [{
                    "arm": "agent",
                    "termination_outcome": agent_data["termination_outcome"],
                    "qualifying": agent_data["successful_screened_products"],
                    "best_delta": agent_data["best_compliant_delta"],
                    "schema_errors": agent_data.get("schema_errors", 0),
                    "state_machine_rejections": agent_data.get("state_machine_rejections", 0),
                }],
                "n_observations": 1,
                "goal_met_count": 1 if agent_data["termination_outcome"] == "goal_met" else 0,
                "execution_failure_count": 1 if agent_data["termination_outcome"] == "execution_failure" else 0,
                "qualifying_total": agent_data["successful_screened_products"],
                "best_delta_max": agent_data["best_compliant_delta"],
            }
    return row


def main():
    rows = []
    for name, smiles, scenario, run_dir in parents_orig:
        agent_data = next((o for o in agent_obs_orig if o["parent"] == name), None)
        if agent_data:
            obs_list = [agent_data[k] for k in ["pre_fix", "fix", "r2", "r3"] if k in agent_data]
            row = build_row(name, smiles, scenario, run_dir, "original_3_parents_2026_09_21", agent_data=obs_list)
        else:
            row = build_row(name, smiles, scenario, run_dir, "original_3_parents_2026_09_21")
        rows.append(row)

    for name, smiles, scenario, run_dir in parents_p1:
        agent = load_agent_p1(run_dir)
        row = build_row(name, smiles, scenario, run_dir, "P1_multi_parent_2026_09_22", agent_data=agent)
        rows.append(row)

    summary = {
        "schema_version": 3,
        "experiment": "diagnostic_2d_baseline_vs_agent_8parents_20260922",
        "created_utc": "2026-09-22T22:15:00+00:00",
        "purpose": (
            "Extend the P3 baseline comparison (greedy + random) from the original 3 parents "
            "to all 8 parents tested by the agent. Baselines bypass the LLM harness and drive "
            "evaluate_candidates directly; offline, zero token cost."
        ),
        "frozen_version": (
            "Baselines are reproducible by re-running greedy/random on the frozen manifest "
            "directories. Each agent observation lives in its own run directory."
        ),
        "connectivity_gate": {
            "note": "Baselines are offline, no gate needed. Gate applies only to agent arms."
        },
        "design": {
            "greedy": "sort unique products by (-catalogue_id_count, smiles); evaluate in that order until budget exhausted",
            "random": "deterministic seeded shuffle (seed=42); same evaluation loop",
            "budget": "max_evaluations=11 (1 parent + 10 products); same as agent arm",
            "stop_rule": "after budget: finish with the best qualifying product, or honestly report none",
        },
        "arms_by_parent": rows,
        "tally_by_arm": {
            "greedy_qualifying_total_8p": sum(row["greedy"]["qualifying"] for row in rows),
            "random_qualifying_total_8p": sum(row["random"]["qualifying"] for row in rows),
            "P1_5p": {
                "greedy_qualifying_total": sum(row["greedy"]["qualifying"] for row in rows if row["scenario"] == "parent"),
                "random_qualifying_total": sum(row["random"]["qualifying"] for row in rows if row["scenario"] == "parent"),
                "greedy_parents_with_at_least_one_qualifying": sum(1 for row in rows if row["scenario"] == "parent" and row["greedy"]["qualifying"] > 0),
                "random_parents_with_at_least_one_qualifying": sum(1 for row in rows if row["scenario"] == "parent" and row["random"]["qualifying"] > 0),
            },
            "original_3p": {
                "greedy_qualifying_total": sum(row["greedy"]["qualifying"] for row in rows if row["scenario"] == "phenol"),
                "random_qualifying_total": sum(row["random"]["qualifying"] for row in rows if row["scenario"] == "phenol"),
                "agent_goal_met_total": sum(row["agent"]["goal_met_count"] for row in rows if row["scenario"] == "phenol" and "agent" in row),
            },
        },
        "headline_finding": (
            "On the original 3 parents (phenol scenario, 24 actions / 16 products), the agent "
            "had a clear advantage: greedy 0/3, random 3/9 qualifying products, agent 7/12 "
            "goal_met observations (toluene 4/4). On the 5 P1 parents (parent scenario, 70 actions "
            "/ 40 products), the picture inverts: greedy 7 qualifying products across 3/5 parents "
            "(catechol, resorcinol, benzonitrile), random 7 across 4/5 parents (4-methylphenol, "
            "4-fluorophenol, benzonitrile, catechol). The agent has only n=1 per P1 parent, so a "
            "single observation cannot establish a stable advantage; the earlier 'agent > random "
            ">> greedy' claim is true ONLY for the original 3 parents, not the extended 8-parent set."
        ),
        "intent_check": (
            "Same 0.01 property_score threshold, same hERG proxy, same evaluator as the agent arm. "
            "Baselines do not call the LLM. Free-text chemical claims require separate manual audit."
        ),
    }

    out_path = "runs/samples/diagnostic_2d_baseline_vs_agent_8parents_20260922_summary.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"Wrote {out_path}")
    print()
    print(f"{'parent':14s} {'scenario':9s} | {'g_qual':>6s} {'g_best':>8s} | {'r_qual':>6s} {'r_best':>8s} | {'a_obs':>5s} {'a_gm':>5s} {'a_qual':>6s} {'a_best':>8s}")
    print("-" * 100)
    for row in rows:
        g = row["greedy"]
        r = row["random"]
        a = row.get("agent", {})
        aq_total = a.get("qualifying_total", "-") if a else "-"
        ab_max = f"{a['best_delta_max']:.5f}" if a else "-"
        a_n = a.get("n_observations", "-") if a else "-"
        a_gm = a.get("goal_met_count", "-") if a else "-"
        print(f"{row['parent']:14s} {row['scenario']:9s} | {g['qualifying']:>6d} {g['best_delta']:>8.5f} | {r['qualifying']:>6d} {r['best_delta']:>8.5f} | {str(a_n):>5s} {str(a_gm):>5s} {str(aq_total):>6s} {ab_max:>8s}")


if __name__ == "__main__":
    main()