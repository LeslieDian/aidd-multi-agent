from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml
from agents.harness import TaskState, CheckpointStore, Harness
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints
from agents.harness.planning import preview, experience
from agents.harness.tools import default_registry
from scripts.validate_agent_decisions import action, ScriptedPolicy


def setup(tmp_path):
    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    state = TaskState(goal="Improve properties", config=config, mock=True, max_steps=30,
        constraints=normalize_constraints({"require_planned_edits": True, "require_meaningful_improvement": True,
                                           "allowed_parent_atom_indices": [6]}))
    add_seed_candidates(state, ["CCOc1ccccc1"])
    store = CheckpointStore(tmp_path)
    store.save(state)
    return store, Harness(store, ScriptedPolicy([action("evaluate", candidate_ids=["c1"])] )).run(1)


def option(fragment="C", site=6):
    return {"edit": {"operation": "attach_fragment", "arguments": {"atom_index": site,
            "fragment_smiles": fragment, "fragment_atom_index": 0}},
            "rationale": "Compare fragment hypotheses", "expected_benefit": "property improvement, unverified",
            "allowed_cost": "No risk increase", "expected_metric": "property_score", "expected_direction": "increase",
            "predictions": [{"metric": "property_score", "direction": "increase", "min_change": .01}]}


def propose(state, tmp_path, options=None):
    return default_registry().execute(state, action("propose_edits", parent_id="c1",
        options=options or [option(), option("N")]), tmp_path)


def select(state, tmp_path, pid="p1", index=0, evidence=None):
    return default_registry().execute(state, action("select_edit", proposal_id=pid, option_index=index,
        rationale="Choose this over the alternatives based on observed result", evidence_ids=evidence or []), tmp_path)


def test_preview_is_nonmutating_and_reports_errors(tmp_path):
    _, state = setup(tmp_path)
    original = deepcopy(state)
    assert preview(state, "c1", option()["edit"])["passed"]
    assert not preview(state, "c1", option(site=0)["edit"])["passed"]
    assert not preview(state, "c1", option(site=999)["edit"])["passed"]
    assert not preview(state, "c1", option("bad smiles")["edit"])["passed"]
    assert not preview(state, "c1", option(site=2)["edit"])["passed"]  # oxygen valence
    assert state == original


def test_invalid_alternative_retained_but_cannot_execute(tmp_path):
    _, state = setup(tmp_path)
    result = propose(state, tmp_path, [option(site=999), option()])
    assert not result["options"][0]["precheck"]["passed"]
    assert experience(state)[0]["evidence_id"] == "p:p1:0"
    with pytest.raises(ValueError, match="failed feasibility"):
        select(state, tmp_path)
    chosen = select(state, tmp_path, index=1)
    result = default_registry().execute(state, action("execute_selected_edit", selection_id=chosen["selection_id"]), tmp_path)
    assert result["added_ids"] == ["c2"]
    assert state.edit_selections["s1"]["status"] == "executed"
    assert not preview(state, "c1", option()["edit"])["passed"]
    with pytest.raises(ValueError, match="already executed"):
        default_registry().execute(state, action("execute_selected_edit", selection_id="s1"), tmp_path)


def test_stale_selection_and_budget_cannot_bypass(tmp_path):
    _, state = setup(tmp_path)
    propose(state, tmp_path)
    select(state, tmp_path)
    with pytest.raises(ValueError, match="already selected"):
        select(state, tmp_path)
    state.update_constraints({"max_edits": 0})
    with pytest.raises(ValueError, match="stale"):
        default_registry().execute(state, action("execute_selected_edit", selection_id="s1"), tmp_path)
    propose(state, tmp_path)
    select(state, tmp_path, pid="p2")
    with pytest.raises(ValueError, match="budget exhausted"):
        default_registry().execute(state, action("execute_selected_edit", selection_id="s2"), tmp_path)
    assert len(state.candidates) == 1


def test_experience_selection_resume_and_exact_action(tmp_path):
    store, state = setup(tmp_path)
    propose(state, tmp_path)
    select(state, tmp_path)
    store.save(state)
    state = Harness(CheckpointStore(tmp_path), ScriptedPolicy([
        action("execute_selected_edit", selection_id="s1"), action("evaluate", candidate_ids=["c2"]),
        action("compare_parent_child", candidate_ids=["c2"]),
        action("choose_strategy", hypothesis_id="planned_s1", choice="rollback", parent_id="c1", rationale="Tiny change")])).run(4)
    assert experience(state)[0]["assessment"]["outcome"] == "inconclusive"
    propose(state, tmp_path, [option("N"), option("O")])
    with pytest.raises(ValueError, match="most recent"):
        select(state, tmp_path, pid="p2")
    select(state, tmp_path, pid="p2", evidence=["h:planned_s1"])
    assert state.edit_selections["s2"]["evidence_snapshot"][0]["historical_outcome"] == "inconclusive"
    with pytest.raises(ValueError, match="exactly the selected"):
        default_registry().execute(state, action("attach_fragment", parent_id="c1", hypothesis_id="planned_s2",
            atom_index=6, fragment_smiles="O", fragment_atom_index=0), tmp_path)


def test_selection_requires_parent_evaluation(tmp_path):
    _, state = setup(tmp_path)
    propose(state, tmp_path)
    state.candidates["c1"].pop("evaluation_status")
    with pytest.raises(ValueError, match="Evaluate the parent"):
        select(state, tmp_path)


def test_report_explains_exhausted_edit_budget(tmp_path):
    from agents.harness.evidence import build_report
    _, state = setup(tmp_path)
    state.update_constraints({"max_edits": 0})
    report = build_report(state, ["c1"], "Do not claim success")
    assert report["outcome"] == "goal_not_met"
    assert report["stop_reason"] == "edit_budget_exhausted"


def test_seeded_mock_dashboard_flow_uses_planned_tools(tmp_path):
    store, state = setup(tmp_path)
    state.update_constraints({"max_edits": 1})
    store.save(state)
    result = Harness(store).run(10)
    assert result.final and result.final["outcome"] == "goal_not_met"
    assert len(result.edit_selections) == 1
    assert result.edit_selections["s1"]["status"] == "executed"
    assert not any(e["type"] == "error" for e in result.events)
