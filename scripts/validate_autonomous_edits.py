"""One parent, at most three edits, real configured planner; no prescribed edits."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints


def run(output, resume=False):
    store = CheckpointStore(output)
    if store.path.exists() and not resume:
        raise ValueError("Output exists; use a new directory")
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    state = TaskState(goal="在保留母体骨架的前提下改善性质代理分，不允许 hERG 风险上升。自行选择修改位点、片段和操作，比较备选方案并根据实际结果调整。最多执行三次编辑；找到合格候选或没有合理方案时整理报告。不要把代理分改善解释为药效改善。",
        config=config, mock=False, max_steps=32, max_model_calls=45, max_evaluations=14,
        constraints=normalize_constraints({"require_planned_edits": True, "require_option_screening": True, "max_edits": 3,
            "allow_generation": False, "allow_freeform_refine": False,
            "require_verified_refinement": True, "require_meaningful_improvement": True}))
    add_seed_candidates(state, ["CCOc1ccccc1"], source="autonomous_acceptance_user")
    if resume:
        state = store.load()
        if state.dock_enabled or state.constraints.get("max_edits") != 3:
            raise ValueError("Resume only this bounded no-docking acceptance scenario")
        previous = store.directory / "acceptance.json"
        if previous.exists():
            archive = store.directory / f"acceptance_before_resume_step_{state.steps_used}.json"
            if not archive.exists():
                archive.write_text(previous.read_text(encoding="utf-8"), encoding="utf-8")
        state.events.append({"type": "acceptance_resume", "reason": "Resume after planner protocol clarification; budgets and goal unchanged"})
    store.save(state)
    for _ in range(32):
        state = Harness(store).run(1)
        print(json.dumps({"step": state.steps_used, "calls": state.model_calls_used,
                          "reason": state.reason, "candidates": len(state.candidates)}, ensure_ascii=True), flush=True)
        if state.reason != "action_limit":
            break
    edits = [c for c in state.candidates.values() if c.get("candidate_role") == "deterministic_edit"]
    screened = [o["screening"] for p in state.edit_proposals.values() for o in p["options"] if o.get("screening")]
    checks = {"final_report": bool(state.final), "bounded_edits": len(edits) <= 3 and bool(screened),
              "alternatives_recorded": bool(state.edit_proposals),
              "all_edits_selected": all(c.get("hypothesis_id") in state.hypotheses and
                  state.hypotheses[c["hypothesis_id"]].get("selection_id") in state.edit_selections for c in edits),
              "no_docking": not state.dock_enabled,
              "screenings_budgeted": 0 < len(state.option_screenings) <= state.evaluations_used <= state.max_evaluations,
              "score_attribution": bool(screened) and all(s.get("property_attribution", {}).get("status") == "verified" for s in screened) and all(c.get("property_attribution", {}).get("status") == "verified" for c in edits),
              "predictions_checked": bool(screened) and all(s.get("prediction_checks") for s in screened) and all(h.get("prediction_checks") for h in state.hypotheses.values()),
              "selections_cite_prior_results": all(s["evidence_ids"] for s in list(state.edit_selections.values())[1:])}
    summary = {"passed": all(checks.values()), "checks": checks,
               "coverage": {"real_edit_execution": bool(edits), "real_selection": bool(state.edit_selections),
                            "real_hypothesis_comparison": any(h.get("prediction_checks") for h in state.hypotheses.values()),
                            "screening_prediction_checks": bool(screened),
                            "note": "Early stop without committed edits is valid; it does not cover the real execution/reuse path"},
               "model": config["llm"]["providers"][config["harness"]["planner"]]["model"],
               "steps": state.steps_used, "request_attempts": state.model_calls_used,
               "evaluations": state.evaluations_used, "edits": len(edits),
               "screening_records": len(state.option_screenings),
               "outcome": (state.final or {}).get("outcome"), "reason": state.reason,
               "hypotheses": [{"id": h["hypothesis_id"], "outcome": h.get("outcome"),
                               "delta": h.get("observed_delta")} for h in state.hypotheses.values()]}
    (store.directory / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--resume", action="store_true", help="Resume the same acceptance checkpoint without resetting budgets")
    args = parser.parse_args()
    result = run(args.output, args.resume)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
