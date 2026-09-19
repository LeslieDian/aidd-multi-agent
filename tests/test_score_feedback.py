from copy import deepcopy
from pathlib import Path
import pytest
import yaml

from agents.evaluator import evaluate_candidates
from agents.harness import TaskState, CheckpointStore, Harness
from agents.harness.attribution import breakdown, compare_breakdown, check_predictions
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints
from agents.harness.tools import default_registry
from agents.harness.screening import reuse_for_candidate
from scripts.validate_agent_decisions import action, ScriptedPolicy


@pytest.fixture
def config():
    return yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))


def alternatives():
    return [{"edit": {"operation": "attach_fragment", "arguments": {"atom_index": 6, "fragment_smiles": fragment, "fragment_atom_index": 0}},
             "rationale": "Compare two polarities", "expected_benefit": "Unverified property gain", "allowed_cost": "Numerical limits",
             "expected_metric": "property_score", "expected_direction": "increase",
             "predictions": [{"metric": "qed", "direction": "increase", "min_change": .001},
                             {"metric": "sa_score", "direction": "decrease", "min_change": .001}]} for fragment in ("C", "N")]


def create(tmp_path, config, budget=3):
    store = CheckpointStore(tmp_path)
    state = TaskState(goal="Test scored alternatives", config=config, mock=True, max_steps=25, max_evaluations=budget,
        constraints=normalize_constraints({"require_planned_edits": True, "require_option_screening": True,
                                           "require_meaningful_improvement": True}))
    add_seed_candidates(state, ["CCOc1ccccc1"])
    store.save(state)
    return store


def initial_actions():
    return [action("evaluate", candidate_ids=["c1"]), action("propose_edits", parent_id="c1", options=alternatives()),
            action("evaluate_options", proposal_id="p1")]


def test_qed_improves_but_sa_loss_dominates(config):
    parent, child = evaluate_candidates([{"smiles": s} for s in ("CCOc1ccccc1", "CCOc1cccc(F)c1")], config["scoring"], config["target"], dock_enabled=False)
    result = compare_breakdown(parent, child, config["scoring"])
    assert result["balanced"] and result["observed_delta"] < 0
    rows = {r["metric"]: r for r in result["components"]}
    assert rows["admet_quality_score"]["contribution_delta"] > 0
    assert rows["sa_score"]["contribution_delta"] < -rows["admet_quality_score"]["contribution_delta"]
    assert child["admet"]["qed"] > parent["admet"]["qed"]
    assert abs(result["contribution_delta_sum"] - result["observed_delta"]) < 1e-12
    for c in (parent, child):
        b = breakdown(c, config["scoring"])
        assert abs(sum(r["contribution"] for r in b["quality_subcomponents"]) - b["components"][0]["contribution"]) < 1e-12
    child["property_score"] += .1
    assert breakdown(child, config["scoring"])["status"] == "formula_mismatch"


def test_predictions_supported_refuted_and_missing(config):
    parent, child = evaluate_candidates([{"smiles": s} for s in ("CCOc1ccccc1", "CCOc1cccc(F)c1")], config["scoring"], config["target"], dock_enabled=False)
    predictions = [{"metric": name, "direction": direction, "min_change": .001} for name, direction in
                   [("qed", "increase"), ("sa_score", "decrease"), ("vina", "decrease"), ("herg_risk", "decrease")]]
    assert [r["status"] for r in check_predictions(predictions, parent, child)] == ["supported", "refuted", "insufficient_evidence", "inconclusive"]
    child["protocol_id"] = "changed"
    assert all(r["status"] == "insufficient_evidence" for r in check_predictions(predictions, parent, child))


def test_screening_budget_reuse_resume_and_predictions(tmp_path, config):
    store = create(tmp_path, config)
    state = Harness(store, ScriptedPolicy(initial_actions())).run(3)
    assert state.evaluations_used == 3 and len(state.option_screenings) == 2
    assert len(state.candidates) == 1  # alternatives are separate from committed edits
    p = state.edit_proposals["p1"]
    assert p["screening_comparison"]["suggested_order"][0] == 0
    assert "1" in p["screening_comparison"]["rows"][0]["pairwise_similarity"]
    original_predictions = deepcopy(p["options"][0]["predictions"])
    with pytest.raises(ValueError, match="tradeoff"):
        default_registry().execute(state, action("select_edit", proposal_id="p1", option_index=1, rationale="Ignore risk", evidence_ids=[]), tmp_path)
    state = Harness(CheckpointStore(tmp_path), ScriptedPolicy([
        action("select_edit", proposal_id="p1", option_index=0, rationale="Prefer the compliant measured alternative", evidence_ids=[]),
        action("execute_selected_edit", selection_id="s1"), action("evaluate", candidate_ids=["c2"]),
        action("compare_parent_child", candidate_ids=["c2"]), action("finish", candidate_ids=["c2"], summary="No overclaim")])).run(5)
    assert state.evaluations_used == 3
    assert state.candidates["c2"]["screening_reuse"]["new_evaluations"] == 0
    assert state.hypotheses["planned_s1"]["predictions"] == original_predictions
    assert state.hypotheses["planned_s1"]["prediction_checks"]
    assert state.hypotheses["planned_s1"]["attribution_delta"]["balanced"]
    assert state.final["evaluation_accounting"]["budget_used"] == 3
    state.dock_enabled = True
    assert not reuse_for_candidate(state, {"smiles": state.candidates["c2"]["smiles"]})


def test_budget_exhaustion_prevents_hidden_screening(tmp_path, config):
    store = create(tmp_path, config, budget=2)
    state = Harness(store, ScriptedPolicy(initial_actions())).run(3)
    assert state.reason == "evaluation_budget_exhausted"
    assert state.evaluations_used == 1 and not state.option_screenings


def test_repeated_screening_cache_charges_zero_and_selection_gate(tmp_path, config):
    store = create(tmp_path, config)
    state = Harness(store, ScriptedPolicy(initial_actions()[:2])).run(2)
    with pytest.raises(ValueError, match="evaluate_options"):
        default_registry().execute(state, action("select_edit", proposal_id="p1", option_index=0, rationale="Premature", evidence_ids=[]), tmp_path)
    state = Harness(store, ScriptedPolicy([initial_actions()[2]])).run(1)
    registry = default_registry()
    registry.execute(state, action("propose_edits", parent_id="c1", options=alternatives()), tmp_path)
    registry.execute(state, action("evaluate_options", proposal_id="p2"), tmp_path)
    assert state.evaluations_used == 3
    assert state.edit_proposals["p2"]["screening_comparison"]["new_evaluations"] == 0
    assert state.edit_proposals["p1"]["options"][0]["prediction_provenance"]["new_to_task_screening"]
    assert not state.edit_proposals["p2"]["options"][0]["prediction_provenance"]["new_to_task_screening"]


def test_nested_schema_rejects_strings_nonfinite_and_unknown_fields():
    registry = default_registry()
    good = action("propose_edits", parent_id="c1", options=alternatives())
    registry.validate(good)
    invalid = deepcopy(good)
    invalid.pop("reason")
    with pytest.raises(ValueError, match="tool, arguments, reason"):
        registry.validate(invalid)
    invalid = deepcopy(good)
    invalid["arguments"]["options"][0]["predictions"][0]["min_change"] = 0
    with pytest.raises(ValueError, match=r"options\[0\].*minimum=1e-09.*got 0"):
        registry.validate(invalid)
    for value in ("[]", True, {}):
        invalid = deepcopy(good)
        invalid["arguments"]["options"] = value
        with pytest.raises(ValueError):
            registry.validate(invalid)
    invalid = deepcopy(good)
    invalid["arguments"]["options"][0]["predictions"][0]["min_change"] = float("nan")
    with pytest.raises(ValueError):
        registry.validate(invalid)
    invalid = deepcopy(good)
    invalid["arguments"]["options"][0]["edit"]["arguments"]["unexpected"] = 1
    with pytest.raises(ValueError):
        registry.validate(invalid)


def test_planner_gets_components_and_only_applicable_stage_tools(tmp_path, config, monkeypatch):
    import json
    from types import SimpleNamespace
    from agents.harness.runtime import LLMPolicy
    import agents.llm
    store = create(tmp_path, config)
    state = Harness(store, ScriptedPolicy(initial_actions())).run(3)
    for option in state.edit_proposals["p1"]["options"]:
        option["screening"]["effect_assessment"]["outcome"] = "tradeoff_exceeded"
    captured = []
    def chat(system, payload):
        assert "top-level reason is REQUIRED" in system
        assert "Never use min_change=0" in system
        captured.append(json.loads(payload))
        return action("finish", candidate_ids=["c1"], summary="No qualifying screened options")
    monkeypatch.setattr(agents.llm, "get_client", lambda *a, **k: SimpleNamespace(chat_json=chat))
    LLMPolicy().decide(state, default_registry())
    context = captured[0]
    names = {t["name"] for t in context["tools"]}
    assert "choose_strategy" not in names and "select_edit" not in names
    assert "propose_edits" in names and "finish" in names
    assert context["candidates"][0]["validate"]["sa_score"]
    assert context["candidates"][0]["property_attribution"]["status"] == "verified"
