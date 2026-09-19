"""Small, bounded acceptance: no docking; --real uses the configured planner."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints


def action(tool, **arguments):
    return {"tool": tool, "arguments": arguments, "reason": "Bounded acceptance scenario"}


def hypothesis(hid, fragment):
    return action("record_hypothesis", hypothesis_id=hid, parent_id="c1",
                  rationale=f"Test {fragment} at parent atom 6 against property proxy",
                  expected_metric="property_score", expected_direction="increase", allowed_tradeoff="Numerical constraints only",
                  next_if_supported="continue", next_if_not_supported="rollback and change fragment")


class ScriptedPolicy:
    def __init__(self, actions):
        self.actions = iter(actions)

    def decide(self, state, registry):
        return next(self.actions)


def run(directory, real=False):
    directory = Path(directory)
    store = CheckpointStore(directory)
    if store.path.exists():
        raise ValueError("Choose a new output directory; existing evidence is immutable")
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    state = TaskState(goal=("Small acceptance only: evaluate the supplied parent, propose and attach C at parent atom 6, "
        "evaluate and compare. If inconclusive, choose rollback or switch_strategy, then propose and attach N "
        "at atom 6 of the original parent, evaluate and compare. Do not finish before both comparisons. "
        "Use only deterministic edits, obey all numerical limits. No docking."), config=config, mock=not real,
        max_steps=20, max_model_calls=24, max_evaluations=3,
        constraints=normalize_constraints({"allow_generation": False, "allow_freeform_refine": False,
            "require_verified_refinement": True, "require_meaningful_improvement": True,
            "allowed_parent_atom_indices": [6], "max_changed_atoms": 2}))
    add_seed_candidates(state, ["CCOc1ccccc1"], source="acceptance_user")
    store.save(state)
    actions = [action("evaluate", candidate_ids=["c1"]), hypothesis("h1", "C"),
        action("attach_fragment", parent_id="c1", atom_index=6, fragment_smiles="C", fragment_atom_index=0, hypothesis_id="h1"),
        action("evaluate", candidate_ids=["c2"]), action("compare_parent_child", candidate_ids=["c2"]),
        action("choose_strategy", hypothesis_id="h1", choice="rollback", parent_id="c1", rationale="Tiny improvement; change to a polar fragment"),
        hypothesis("h2", "N"),
        action("attach_fragment", parent_id="c1", atom_index=6, fragment_smiles="N", fragment_atom_index=0, hypothesis_id="h2"),
        action("evaluate", candidate_ids=["c3"]), action("compare_parent_child", candidate_ids=["c3"])]
    policy = None if real else ScriptedPolicy(actions)
    for _ in range(14 if real else 10):
        state = Harness(store, policy).run(1)
        assessed = [h for h in state.hypotheses.values() if h.get("status") == "assessed"]
        if len(assessed) >= 2:
            break
        if state.reason != "action_limit":
            break
    first = next((h for h in state.hypotheses.values() if h.get("child_id") == "c2"), {})
    assert first.get("outcome") == "inconclusive", (state.reason, first)
    assert any(s.get("executed_child_id") for s in state.strategies), "Strategy was not executed"
    assert len([h for h in state.hypotheses.values() if h.get("status") == "assessed"]) == 2
    before = deepcopy(state.candidates)
    store.submit_control("constraints", constraints={"allowed_parent_atom_indices": [0], "allow_refine": False})
    Harness(store).apply_controls()
    store.submit_control("steer", instruction="Allowed site is now only atom 0. Editing is disabled. Finish now using existing evidence, explicitly report no qualifying result. Do not claim the tiny improvement is success.")
    Harness(store).apply_controls()
    final_policy = None if real else ScriptedPolicy([action("finish", candidate_ids=["c1", "c2", "c3"], summary="MODEL CLAIM: all edits succeeded")])
    state = Harness(CheckpointStore(directory), final_policy).run(3 if real else 1)
    assert state.final and state.final["outcome"] == "goal_not_met", state.reason
    assert state.final["candidate_ids"] == []
    assert len(state.final["reference_parents"]) == 1
    assert len(state.final["rejected_candidates"]) == 2
    for cid in ("c2", "c3"):
        assert before[cid]["current_validation"]["passed"]
        assert not state.candidates[cid]["current_validation"]["passed"]
        assert state.candidates[cid]["property_score"] == before[cid]["property_score"]
        assert len(state.candidates[cid]["validation_history"]) >= 2
    summary = {"passed": True, "mode": "real" if real else "scripted_offline", "planner": config["harness"]["planner"] if real else None,
        "model": config["llm"]["providers"][config["harness"]["planner"]]["model"] if real else None,
        "external_model_calls": state.model_calls_used if real else 0,
        "steps": state.steps_used, "model_calls": state.model_calls_used, "evaluations": state.evaluations_used,
        "tiny_delta": first["observed_delta"], "tiny_outcome": first["outcome"],
        "strategies": state.strategies, "outcome": state.final["outcome"], "revision": state.revision,
        "checkpoint": str(store.path), "docking": False}
    (directory / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.output, args.real), ensure_ascii=True, indent=2))
