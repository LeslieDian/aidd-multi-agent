import json
from pathlib import Path
import pytest
from scripts.compare_2d_policies import prepare, audit, run_arm, new_state, load_frozen, catalogue, CatalogRegistry, action


def test_frozen_catalogue_and_equal_budgets(tmp_path):
    output = tmp_path / "experiment"
    manifest = prepare(output)
    assert len(catalogue()) == 35
    assert manifest["constraints"]["min_effects"]["property_score"] == .01
    for name in ("rule", "agent"):
        state = new_state(manifest, name)
        assert state.max_evaluations == 11
        assert state.candidates["c1"]["smiles"] == "CCOc1ccccc1"
        assert not state.option_screenings
    assert load_frozen(output)["source_hashes"]
    path = output / "manifest.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest changed"):
        load_frozen(output)


def test_audit_is_separate_and_rule_cannot_overspend(tmp_path):
    output = tmp_path / "experiment"
    manifest = prepare(output)
    reachable = audit(output)
    assert reachable["audit_only_not_arm_budget"]
    assert reachable["audit_evaluations_including_parent"] == reachable["unique_valid_products"] + 1
    fresh = new_state(manifest, "agent")
    assert fresh.evaluations_used == 0
    assert not any("evaluation_status" in c for c in fresh.candidates.values())
    result = run_arm(output, "rule")
    assert result["new_structure_evaluations"] <= 10
    assert result["total_charged_evaluations"] == 1 + result["new_structure_evaluations"]
    assert result["planner_attempts"] == 0
    assert result["rejected_actions"] == 0
    assert result["final_outcome"] in {"goal_met", "goal_not_met"}
    with pytest.raises(ValueError, match="already exists"):
        run_arm(output, "rule")


def test_proposals_outside_shared_catalogue_rejected(tmp_path):
    manifest = prepare(tmp_path / "experiment")
    state = new_state(manifest, "agent")
    options = [{"edit": {"operation": "attach_fragment", "arguments": {"atom_index": 6,
        "fragment_smiles": "Cl", "fragment_atom_index": 0}}, "rationale": "test", "expected_benefit": "test",
        "allowed_cost": "test", "expected_metric": "property_score", "expected_direction": "increase",
        "predictions": [{"metric": "property_score", "direction": "increase", "min_change": .01}]}] * 2
    with pytest.raises(ValueError, match="frozen catalogue"):
        CatalogRegistry(manifest).preflight(state, action("propose_edits", parent_id="c1", options=options))


def test_new_positive_control_is_reachable_and_hides_scores(tmp_path):
    output = tmp_path / "positive_control"
    manifest = prepare(output, scenario="phenol")
    assert manifest["constraints"]["min_effects"]["property_score"] == .01
    with pytest.raises(ValueError, match="requires completed reachability"):
        run_arm(output, "rule")
    result = audit(output)
    assert result["qualifying_products"] > 0
    state = new_state(manifest, "agent")
    assert state.candidates["c1"]["smiles"] == "Oc1ccccc1"
    assert not state.option_screenings
    assert "property_score" not in state.candidates["c1"]
    assert all(set(row) == {"id", "edit"} for row in manifest["catalogue"])
    rule = run_arm(output, "rule")
    assert rule["total_charged_evaluations"] <= 11
