from copy import deepcopy
import pytest
from agents.harness import CheckpointStore
from agents.harness.evidence import judge_effect, build_report, revalidate
from agents.harness.molecule_ops import normalize_constraints, evidence_delta
from agents.harness.tools import default_registry
from scripts.validate_agent_decisions import run, action, hypothesis


def delta(prop=0.02, herg=0.0, same=True):
    return {"property_score": {"delta": prop}, "herg_risk": {"delta": herg},
            "composite_score": {"delta": None}, "vina": {"delta": None}, "same_protocol": same}


@pytest.mark.parametrize("change,herg,same,outcome", [
    (0.000008, 0, True, "inconclusive"), (0.02, 0.1, True, "tradeoff_exceeded"),
    (0.01, 0, True, "supported"), (-0.005, 0, True, "inconclusive"),
    (-0.01, 0, True, "not_supported"), (0.02, None, True, "insufficient_evidence"),
    (0.02, 0, False, "insufficient_evidence"), (None, 0, True, "insufficient_evidence"),
])
def test_effect_decisions(change, herg, same, outcome):
    result = judge_effect(delta(change, herg, same), "property_score", "increase", normalize_constraints({}))
    assert result["outcome"] == outcome


def test_lower_risk_and_numeric_tradeoff():
    rules = normalize_constraints({"max_regressions": {"property_score": 0.01}})
    assert judge_effect(delta(-0.005, -0.03), "herg_risk", "decrease", rules)["outcome"] == "supported"
    assert judge_effect(delta(-0.02, -0.03), "herg_risk", "decrease", rules)["outcome"] == "tradeoff_exceeded"


def test_nonfinite_evidence_is_missing():
    assert evidence_delta({"property_score": 0.1}, {"property_score": float("nan")})["property_score"]["delta"] is None


def test_complete_pause_intervention_scenario(tmp_path):
    result = run(tmp_path / "scenario")
    assert result["passed"] and result["external_model_calls"] == 0
    state = CheckpointStore(tmp_path / "scenario").load()
    assert "MODEL CLAIM" not in state.final["summary"]
    assert state.final["model_explanation"]["verified"] is False
    assert state.candidates["c2"]["validation_history"][0]["passed"] is True
    assert state.candidates["c2"]["current_improvement"]["outcome"] == "edit_rejected"
    historical = deepcopy(state.candidates["c2"]["validation_history"])
    revalidate(state)
    assert state.candidates["c2"]["validation_history"] == historical
    # Previously selected mother remains a reference even if all completion restrictions are removed.
    state.constraints = normalize_constraints({})
    report = build_report(state, ["c1"], "Mother proves success")
    assert "c1" not in report["candidate_ids"]


def test_strategy_gate_and_repeated_edit(tmp_path):
    run(tmp_path / "scenario")
    state = CheckpointStore(tmp_path / "scenario").load()
    state.update_constraints({"allowed_parent_atom_indices": [6], "allow_refine": True})
    registry = default_registry()
    with pytest.raises(ValueError, match="Continue requires"):
        registry.execute(state, action("choose_strategy", hypothesis_id="h1", choice="continue", parent_id="c2", rationale="pretend improvement"), tmp_path)
    with pytest.raises(ValueError, match="choose_strategy"):
        registry.execute(state, hypothesis("h3", "C"), tmp_path)
    registry.execute(state, action("choose_strategy", hypothesis_id="h2", choice="rollback", parent_id="c1", rationale="Try a different edit"), tmp_path)
    registry.execute(state, hypothesis("h3", "C"), tmp_path)
    with pytest.raises(ValueError, match="Repeated molecular edit"):
        registry.execute(state, action("attach_fragment", parent_id="c1", atom_index=6, fragment_smiles="C", fragment_atom_index=0, hypothesis_id="h3"), tmp_path)
    state.update_constraints({"max_changed_atoms": 1})
    with pytest.raises(ValueError, match="stale"):
        registry.execute(state, action("attach_fragment", parent_id="c1", atom_index=6, fragment_smiles="O", fragment_atom_index=0, hypothesis_id="h3"), tmp_path)


@pytest.mark.parametrize("changes", [{"max_regressions": {"herg_risk": -1}}, {"min_effects": {"property_score": 0}},
                                        {"max_regressions": {"herg_risk": float("inf")}}])
def test_bad_limits_rejected(changes):
    with pytest.raises(ValueError):
        normalize_constraints(changes)
